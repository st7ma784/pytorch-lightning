# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import multiprocessing
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, List, Optional, Tuple, Union
from weakref import proxy

from torch.utils.data import DataLoader, RandomSampler, Sampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler

import pytorch_lightning as pl
from pytorch_lightning.accelerators.ipu import IPUAccelerator
from pytorch_lightning.overrides.distributed import UnrepeatedDistributedSampler
from pytorch_lightning.strategies import DDPSpawnStrategy
from pytorch_lightning.trainer.states import RunningStage, TrainerFn
from pytorch_lightning.trainer.supporters import CombinedLoader, CycleIterator
from pytorch_lightning.utilities.apply_func import apply_to_collection
from pytorch_lightning.utilities.auto_restart import _validate_fault_tolerant_automatic
from pytorch_lightning.utilities.data import (
    _auto_add_worker_init_fn,
    _is_dataloader_shuffled,
    _replace_dataloader_init_method,
    _update_dataloader,
    has_iterable_dataset,
    has_len_all_ranks,
)
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.imports import _fault_tolerant_training
from pytorch_lightning.utilities.model_helpers import is_overridden
from pytorch_lightning.utilities.rank_zero import rank_zero_deprecation, rank_zero_warn
from pytorch_lightning.utilities.types import EVAL_DATALOADERS, TRAIN_DATALOADERS
from pytorch_lightning.utilities.warnings import PossibleUserWarning, WarningCache

warning_cache = WarningCache()


class DataConnector:
    def __init__(self, trainer: "pl.Trainer", multiple_trainloader_mode: str = "max_size_cycle"):
        self.trainer = trainer
        self.multiple_trainloader_mode = multiple_trainloader_mode
        self._train_dataloader_source = _DataLoaderSource(None, "")
        self._val_dataloader_source = _DataLoaderSource(None, "")
        self._test_dataloader_source = _DataLoaderSource(None, "")
        self._predict_dataloader_source = _DataLoaderSource(None, "")

        self._datahook_selector = _DataHookSelector(None, None)

    @property
    def _should_reload_train_dl(self) -> bool:
        """Check if train dataloader should be reloaded."""
        n_epochs = self.trainer.reload_dataloaders_every_n_epochs
        return n_epochs and (self.trainer.current_epoch - self.trainer._last_train_dl_reload_epoch >= n_epochs)

    @property
    def _should_reload_val_dl(self) -> bool:
        """Check if validation dataloader should be reloaded."""
        n_epochs = self.trainer.reload_dataloaders_every_n_epochs
        return n_epochs and (self.trainer.current_epoch - self.trainer._last_val_dl_reload_epoch >= n_epochs)

    def on_trainer_init(
        self,
        check_val_every_n_epoch: int,
        reload_dataloaders_every_n_epochs: int,
        prepare_data_per_node: Optional[bool] = None,
    ) -> None:
        self.trainer.datamodule = None

        if prepare_data_per_node is not None:
            rank_zero_deprecation(
                "Setting `prepare_data_per_node` with the trainer flag is deprecated in v1.5.0 and will be removed in"
                " v1.7.0. Please set `prepare_data_per_node` in `LightningDataModule` and/or `LightningModule`"
                " directly instead."
            )
        self.trainer.prepare_data_per_node = prepare_data_per_node

        if not isinstance(check_val_every_n_epoch, int):
            raise MisconfigurationException(
                f"check_val_every_n_epoch should be an integer. Found {check_val_every_n_epoch}"
            )

        self.trainer.check_val_every_n_epoch = check_val_every_n_epoch

        if not isinstance(reload_dataloaders_every_n_epochs, int) or (reload_dataloaders_every_n_epochs < 0):
            raise MisconfigurationException(
                f"`reload_dataloaders_every_n_epochs` should be an int >= 0, got {reload_dataloaders_every_n_epochs}."
            )

        self.trainer.reload_dataloaders_every_n_epochs = reload_dataloaders_every_n_epochs
        self.trainer._is_data_prepared = False

    def prepare_data(self) -> None:
        # on multi-gpu jobs we only want to manipulate (download, etc) on node_rank=0, local_rank=0
        # or in the case where each node needs to do its own manipulation in which case just local_rank=0
        local_rank_zero = self.trainer.local_rank == 0
        global_rank_zero = self.trainer.local_rank == 0 and self.trainer.node_rank == 0

        datamodule = self.trainer.datamodule
        lightning_module = self.trainer.lightning_module
        # handle datamodule prepare data:
        # check for prepare_data_per_node & datamodule lifecycle properties before calling datamodule.prepare_data
        if datamodule is not None:
            dm_prepare_data_per_node = datamodule.prepare_data_per_node
            dm_eq_prepare_data = datamodule.prepare_data_per_node == self.trainer.prepare_data_per_node
            if self.trainer.prepare_data_per_node is not None and not dm_eq_prepare_data:
                raise MisconfigurationException(
                    "Inconsistent settings found for `prepare_data_per_node`."
                    f" Value was set with both `Trainer(prepare_data_per_node={self.trainer.prepare_data_per_node}.)`"
                    f" and `DataModule.prepare_data_per_node={datamodule.prepare_data_per_node}`."
                    " Move `prepare_data_per_node` setting to DataModule property."
                )
            if (dm_prepare_data_per_node and local_rank_zero) or (not dm_prepare_data_per_node and global_rank_zero):
                self.trainer.datamodule.prepare_data()
        # handle lightning module prepare data:
        # check for prepare_data_per_node before calling lightning_module.prepare_data
        if lightning_module is not None:
            lm_prepare_data_per_node = lightning_module.prepare_data_per_node
            lm_eq_prepare_data = lightning_module.prepare_data_per_node == self.trainer.prepare_data_per_node
            if (self.trainer.prepare_data_per_node is not None) and not lm_eq_prepare_data:
                raise MisconfigurationException(
                    "Inconsistent settings found for `prepare_data_per_node`."
                    f" Value was set with both `Trainer(prepare_data_per_node={self.trainer.prepare_data_per_node}.)`"
                    f" and `LightningModule.prepare_data_per_node={lightning_module.prepare_data_per_node}`."
                    " Move `prepare_data_per_node` setting to LightningModule property."
                )
            if (lm_prepare_data_per_node and local_rank_zero) or (not lm_prepare_data_per_node and global_rank_zero):
                self.trainer._call_lightning_module_hook("prepare_data")
                self.trainer._is_data_prepared = True

    def attach_data(
        self,
        model: "pl.LightningModule",
        train_dataloaders: Optional[TRAIN_DATALOADERS] = None,
        val_dataloaders: Optional[EVAL_DATALOADERS] = None,
        test_dataloaders: Optional[EVAL_DATALOADERS] = None,
        predict_dataloaders: Optional[EVAL_DATALOADERS] = None,
        datamodule: Optional["pl.LightningDataModule"] = None,
    ) -> None:
        # set up the passed in dataloaders (if needed)
        self.attach_dataloaders(
            model,
            train_dataloaders=train_dataloaders,
            val_dataloaders=val_dataloaders,
            test_dataloaders=test_dataloaders,
            predict_dataloaders=predict_dataloaders,
        )
        self.attach_datamodule(model, datamodule=datamodule)
        # set local properties on the model
        self._copy_trainer_model_properties(model)

    def _copy_trainer_model_properties(self, model):
        ref_model = self.trainer.lightning_module or model

        for m in [model, ref_model]:
            m.trainer = proxy(self.trainer)
            # Remove setting use_amp in v1.8
            m._use_amp = self.trainer.amp_backend is not None
            m.precision = self.trainer.precision

    def attach_dataloaders(
        self,
        model: "pl.LightningModule",
        train_dataloaders: Optional[TRAIN_DATALOADERS] = None,
        val_dataloaders: Optional[EVAL_DATALOADERS] = None,
        test_dataloaders: Optional[EVAL_DATALOADERS] = None,
        predict_dataloaders: Optional[EVAL_DATALOADERS] = None,
    ) -> None:
        self.trainer.train_dataloader = None
        self.trainer.val_dataloaders = None
        self.trainer.test_dataloaders = None
        self.trainer.predict_dataloaders = None

        self._train_dataloader_source = _DataLoaderSource(
            train_dataloaders if train_dataloaders is not None else model, "train_dataloader"
        )
        self._val_dataloader_source = _DataLoaderSource(
            val_dataloaders if val_dataloaders is not None else model, "val_dataloader"
        )
        self._test_dataloader_source = _DataLoaderSource(
            test_dataloaders if test_dataloaders is not None else model, "test_dataloader"
        )
        self._predict_dataloader_source = _DataLoaderSource(
            predict_dataloaders if predict_dataloaders is not None else model, "predict_dataloader"
        )

    def attach_datamodule(
        self, model: "pl.LightningModule", datamodule: Optional["pl.LightningDataModule"] = None
    ) -> None:
        # If we have a datamodule, attach necessary hooks + dataloaders
        self._datahook_selector = _DataHookSelector(model, datamodule)

        if datamodule is None:
            return

        self._train_dataloader_source = _DataLoaderSource(datamodule, "train_dataloader")
        self._val_dataloader_source = _DataLoaderSource(datamodule, "val_dataloader")
        self._test_dataloader_source = _DataLoaderSource(datamodule, "test_dataloader")
        self._predict_dataloader_source = _DataLoaderSource(datamodule, "predict_dataloader")

        self.trainer.datamodule = datamodule
        datamodule.trainer = self.trainer

    def _worker_check(self, dataloader: DataLoader, name: str) -> None:
        if not isinstance(dataloader, DataLoader):
            return

        using_spawn = isinstance(self.trainer.strategy, DDPSpawnStrategy)
        num_cpus = multiprocessing.cpu_count()

        # ddp_spawn + num_workers > 0 don't mix! tell the user
        if dataloader.num_workers > 0 and using_spawn:
            # checks for the attr persistent_workers available in pytorch >= 1.7
            if hasattr(dataloader, "persistent_workers"):
                if not dataloader.persistent_workers:
                    rank_zero_warn(
                        "num_workers>0, persistent_workers=False, and strategy=ddp_spawn"
                        " may result in data loading bottlenecks."
                        " Consider setting persistent_workers=True"
                        " (this is a limitation of Python .spawn() and PyTorch)"
                    )
            else:
                rank_zero_warn(
                    "num_workers>0 and strategy=ddp_spawn do not mix well"
                    " and may result in data loading bottlenecks."
                    " Consider setting strategy=ddp to use num_workers>0"
                    " (this is a limitation of Python .spawn() and PyTorch)"
                )

        elif dataloader.num_workers == 0 and using_spawn:
            # checks for the attr persistent_workers available in pytorch >= 1.7
            if hasattr(dataloader, "persistent_workers"):
                if not dataloader.persistent_workers:
                    rank_zero_warn(
                        "strategy=ddp_spawn and num_workers=0 may result in data loading bottlenecks."
                        " Consider setting num_workers>0 and persistent_workers=True"
                    )
            else:
                rank_zero_warn(
                    "strategy=ddp_spawn and num_workers=0 may result in data loading bottlenecks."
                    " Consider setting strategy=ddp and set num_workers>0"
                )

        elif dataloader.num_workers <= 2 < num_cpus and not using_spawn:
            # if changed, update the `filterwarnings` snippet in 'speed.html#num-workers'
            rank_zero_warn(
                f"The dataloader, {name}, does not have many workers which may be a bottleneck."
                " Consider increasing the value of the `num_workers` argument`"
                f" (try {num_cpus} which is the number of cpus on this machine)"
                " in the `DataLoader` init to improve performance.",
                category=PossibleUserWarning,
            )

    def _requires_distributed_sampler(self, dataloader) -> bool:
        return (
            self.trainer._accelerator_connector.replace_sampler_ddp
            and self.trainer._accelerator_connector.is_distributed
            and not isinstance(dataloader.sampler, DistributedSampler)
            and not has_iterable_dataset(dataloader)
            # `DistributedSampler` is never used with `poptorch.DataLoader`
            and not isinstance(self.trainer.accelerator, IPUAccelerator)
        )

    # TODO: shuffle here is kept for BC. Remove it once data_loading.py is removed (#11248)
    def _prepare_dataloader(
        self, dataloader: Any, shuffle: Optional[bool] = None, mode: Optional[RunningStage] = None
    ) -> Any:
        """This function handles to following functionalities:

        - Injecting a `DistributedDataSampler` into the `DataLoader` if on a distributed environment
        - Wrapping the datasets and samplers into fault-tolerant components
        """
        if isinstance(dataloader, CombinedLoader):
            # apply `_prepare_dataloader` on all the collection of loaders
            dataloader.loaders = apply_to_collection(
                dataloader.loaders, (DataLoader, CycleIterator), self._prepare_dataloader, shuffle, mode=mode
            )
            # the length need to recomputed across all dataloaders in case of special behavior.
            dataloader._apply_cycle_iterator_length()
            return dataloader

        # don't do anything if it's not a dataloader
        if not isinstance(dataloader, (DataLoader, CycleIterator)):
            return dataloader

        cycle_iterator: Optional[CycleIterator] = None

        if isinstance(dataloader, CycleIterator):
            cycle_iterator = dataloader
            dataloader = dataloader.loader

        if (
            _fault_tolerant_training()  # injects components to track the state
            or self._requires_distributed_sampler(dataloader)  # sets the distributed sampler
            or mode == RunningStage.PREDICTING  # to track indices for the predictions
            # IPUs use a custom `poptorch.DataLoader` which we might need to convert to
            or isinstance(self.trainer.accelerator, IPUAccelerator)
        ):
            if shuffle is None:
                # for training, set to True always
                # for evaluation, decide based on existing sampler
                shuffle = True if mode == RunningStage.TRAINING else _is_dataloader_shuffled(dataloader)

            sampler = self._resolve_sampler(dataloader, shuffle=shuffle, mode=mode)
            dataloader = _update_dataloader(dataloader, sampler, mode=mode)

        if cycle_iterator is not None:
            cycle_iterator.loader = dataloader
            return cycle_iterator

        return dataloader

    def _resolve_sampler(self, dataloader: DataLoader, shuffle: bool, mode: Optional[RunningStage] = None) -> Sampler:
        if self._requires_distributed_sampler(dataloader):
            if not isinstance(dataloader.sampler, (SequentialSampler, RandomSampler)):
                raise MisconfigurationException(
                    "You seem to have configured a sampler in your DataLoader. This will be replaced"
                    " by `DistributedSampler` since `replace_sampler_ddp` is True and you are using"
                    " distributed training. Either remove the sampler from your DataLoader or set"
                    " `replace_sampler_ddp=False` if you want to use your custom sampler."
                )
            sampler = self._get_distributed_sampler(
                dataloader,
                shuffle,
                mode=mode,
                overfit_batches=self.trainer.overfit_batches,
                **self.trainer.distributed_sampler_kwargs,
            )

            # update docs too once this is resolved
            trainer_fn = self.trainer.state.fn
            if isinstance(sampler, DistributedSampler) and trainer_fn in (TrainerFn.VALIDATING, TrainerFn.TESTING):
                rank_zero_warn(
                    f"Using `DistributedSampler` with the dataloaders. During `trainer.{trainer_fn.value}()`,"
                    " it is recommended to use `Trainer(devices=1)` to ensure each sample/batch gets evaluated"
                    " exactly once. Otherwise, multi-device settings use `DistributedSampler` that replicates"
                    " some samples to make sure all devices have same batch size in case of uneven inputs.",
                    category=PossibleUserWarning,
                )

            return sampler

        return dataloader.sampler

    @staticmethod
    def _get_distributed_sampler(
        dataloader: DataLoader,
        shuffle: bool,
        overfit_batches: Union[int, float],
        mode: Optional[RunningStage] = None,
        **kwargs: Any,
    ) -> DistributedSampler:
        """This function is used to created the distributed sampler injected within the user DataLoader."""
        kwargs["shuffle"] = shuffle and not overfit_batches
        kwargs.setdefault("seed", int(os.getenv("PL_GLOBAL_SEED", 0)))
        cls = UnrepeatedDistributedSampler if mode == RunningStage.PREDICTING else DistributedSampler
        sampler = cls(dataloader.dataset, **kwargs)
        return sampler

    def _reset_eval_dataloader(
        self, mode: RunningStage, model: Optional["pl.LightningModule"] = None
    ) -> Tuple[List[Union[int, float]], List[DataLoader]]:
        """Generic method to reset a dataloader for evaluation.

        Args:
            mode: The running stage of the ``Trainer``
            model: The ``LightningModule`` if calling this outside of the trainer scope.

        Returns:
            Tuple (num_batches, dataloaders)
        """
        assert mode.evaluating or mode == RunningStage.PREDICTING

        # always get the loaders first so we can count how many there are
        dataloaders = self._request_dataloader(mode, model=model)

        if not isinstance(dataloaders, list):
            dataloaders = [dataloaders]

        if any(dl is None for dl in dataloaders):
            rank_zero_warn("One of given dataloaders is None and it will be skipped.")

        for loader in dataloaders:
            apply_to_collection(
                loader.loaders if isinstance(loader, CombinedLoader) else loader,
                DataLoader,
                self._check_eval_shuffling,
                mode=mode,
            )

        # add samplers
        dataloaders = [self._prepare_dataloader(dl, mode=mode) for dl in dataloaders if dl is not None]

        # add worker_init_fn for correct seeding in worker processes
        apply_to_collection(
            dataloaders, dtype=DataLoader, function=_auto_add_worker_init_fn, rank=self.trainer.global_rank
        )

        loader_num_batches = []

        # determine number of batches
        # datasets could be none, 1 or 2+
        module = model or self.trainer.lightning_module or self.datamodule
        if len(dataloaders) != 0:
            for i, dataloader in enumerate(dataloaders):
                orig_num_batches = num_batches = (
                    len(dataloader) if has_len_all_ranks(dataloader, self.trainer.strategy, module) else float("inf")
                )
                self._worker_check(dataloader, f"{mode.dataloader_prefix}_dataloader {i}")

                # percent or num_steps
                limit_eval_batches = getattr(self.trainer, f"limit_{mode.dataloader_prefix}_batches")

                # limit num batches either as a percent or num steps
                if isinstance(limit_eval_batches, int):
                    num_batches = min(num_batches, int(limit_eval_batches))
                elif num_batches != float("inf"):
                    num_batches = int(num_batches * limit_eval_batches)
                elif limit_eval_batches != 1.0:
                    raise MisconfigurationException(
                        f"When using an IterableDataset for `limit_{mode}_batches`,"
                        f" `Trainer(limit_{mode.dataloader_prefix}_batches)` must be `1.0` or an int. An int k"
                        f" specifies `num_{mode.dataloader_prefix}_batches` to use."
                    )

                if num_batches == 0 and limit_eval_batches > 0.0 and isinstance(limit_eval_batches, float):
                    min_pct = 1.0 / len(dataloader)
                    raise MisconfigurationException(
                        f"You requested to check {limit_eval_batches} of the `{mode.dataloader_prefix}_dataloader` but"
                        f" {limit_eval_batches} * {orig_num_batches} < 1. Please increase the"
                        f" `limit_{mode.dataloader_prefix}_batches` flag. Try at least"
                        f" `limit_{mode.dataloader_prefix}_batches={min_pct}`"
                    )

                loader_num_batches.append(num_batches)

        return loader_num_batches, dataloaders

    def _request_dataloader(
        self, stage: RunningStage, model: Optional["pl.LightningModule"] = None
    ) -> Union[DataLoader, List[DataLoader]]:
        """Requests a dataloader from the given model by calling dataloader hooks corresponding to the given stage.

        Returns:
            The requested dataloader
        """
        source = getattr(self, f"_{stage.dataloader_prefix}_dataloader_source")

        hook = f"{stage.dataloader_prefix}_dataloader"
        self.trainer._call_lightning_module_hook("on_" + hook, pl_module=model)
        with _replace_dataloader_init_method():
            # under this context manager, the arguments passed to `DataLoader.__init__` will be captured and saved as
            # attributes on the instance in case the dataloader needs to be re-instantiated later by Ligtning
            dataloader = source.dataloader()
        if isinstance(dataloader, tuple):
            dataloader = list(dataloader)
        self.trainer.strategy.barrier("get_dataloaders")
        _validate_fault_tolerant_automatic(dataloader, stage)
        return dataloader

    @staticmethod
    def _resolve_overfit_batches(dataloader: Collection[DataLoader]) -> Collection[DataLoader]:
        all_have_sequential_sampler = True

        def resolve_has_no_sequential_sampler(dataloader: DataLoader):
            nonlocal all_have_sequential_sampler
            all_have_sequential_sampler = all_have_sequential_sampler & isinstance(
                dataloader.sampler, SequentialSampler
            )

        apply_to_collection(dataloader, DataLoader, resolve_has_no_sequential_sampler)

        if not all_have_sequential_sampler:
            rank_zero_warn(
                "You requested to overfit but enabled training dataloader shuffling."
                " We are turning off the training dataloader shuffling for you."
            )

            def replace_sampler(dataloader: DataLoader) -> DataLoader:
                return _update_dataloader(dataloader, SequentialSampler(dataloader.dataset), mode=RunningStage.TRAINING)

            dataloader = apply_to_collection(dataloader, DataLoader, replace_sampler)

        return dataloader

    @staticmethod
    def _check_eval_shuffling(dataloader, mode):
        if _is_dataloader_shuffled(dataloader):
            rank_zero_warn(
                f"Your `{mode.dataloader_prefix}_dataloader` has `shuffle=True`,"
                " it is strongly recommended that you turn this off for val/test/predict dataloaders.",
                category=PossibleUserWarning,
            )


@dataclass
class _DataLoaderSource:
    """Stores the information where the dataloaders come from.

    The source can be

    1. from a ``*_datalaoder()`` method on the :class:`~pytorch_lightning.core.lightning.LightningModule`,
    2. from a ``*_datalaoder()`` method on the :class:`~pytorch_lightning.core.datamodule.LightningDataModule`,
    3. a direct instance of a :class:`~torch.utils.data.DataLoader` or supported collections thereof.

    Arguments:
        instance: A LightningModule, LightningDataModule, or (a collection of) dataloader(s).
        name: A name for this dataloader source. If the instance is a module, the name corresponds to the hook
            that returns the desired dataloader(s).
    """

    instance: Optional[Union[TRAIN_DATALOADERS, EVAL_DATALOADERS, "pl.LightningModule", "pl.LightningDataModule"]]
    name: str

    def dataloader(self) -> Union[TRAIN_DATALOADERS, EVAL_DATALOADERS]:
        """Returns the dataloader from the source.

        If the source is a module, the method with the corresponding :attr:`name` gets called.
        """
        from pytorch_lightning import LightningDataModule, LightningModule  # prevent cyclic import

        if not self.name:
            return self.instance

        if isinstance(self.instance, LightningModule):
            return self.instance.trainer._call_lightning_module_hook(self.name, pl_module=self.instance)

        if isinstance(self.instance, LightningDataModule):
            method = getattr(self.instance, self.name)
            return method()

        return self.instance

    def is_defined(self) -> bool:
        """Returns whether the source dataloader can be retrieved or not.

        If the source is a module it checks that the method with given :attr:`name` is overridden.
        """
        return not self.is_module() or is_overridden(self.name, self.instance)

    def is_module(self) -> bool:
        """Returns whether the the DataLoader source is a LightningModule or a LightningDataModule.

        It does not check whether ``*_dataloader`` methods are actually overridden.
        """
        from pytorch_lightning import LightningDataModule, LightningModule  # prevent cyclic import

        return isinstance(self.instance, (LightningModule, LightningDataModule))


@dataclass
class _DataHookSelector:
    """Stores the info about the shared DataHooks within LightningModule and LightningDataModule.

    The hook source can be

    1. a method from the :class:`~pytorch_lightning.core.lightning.LightningModule`,
    2. a method from the :class:`~pytorch_lightning.core.datamodule.LightningDataModule`,

    Arguments:
        model: A LightningModule
        datamodule: A LightningDataModule
    """

    model: "pl.LightningModule"
    datamodule: Optional["pl.LightningDataModule"]
    _valid_hooks: Tuple[str] = field(
        default=("on_before_batch_transfer", "transfer_batch_to_device", "on_after_batch_transfer")
    )

    def get_hook(self, hook_name: str) -> Callable:
        if hook_name not in self._valid_hooks:
            raise ValueError(
                f"`{hook_name}` is not a shared hook within `LightningModule` and `LightningDataModule`."
                f" Valid hooks are {self._valid_hooks}."
            )

        if self.datamodule is None:
            return getattr(self.model, hook_name)

        if is_overridden(hook_name, self.datamodule):
            if is_overridden(hook_name, self.model):
                warning_cache.warn(
                    f"You have overridden `{hook_name}` in both `LightningModule` and `LightningDataModule`."
                    " It will use the implementation from `LightningDataModule` instance."
                )
            return getattr(self.datamodule, hook_name)

        if is_overridden(hook_name, self.model):
            warning_cache.warn(
                f"You have overridden `{hook_name}` in `LightningModule` but have passed in a"
                " `LightningDataModule`. It will use the implementation from `LightningModule` instance."
            )
        return getattr(self.model, hook_name)
