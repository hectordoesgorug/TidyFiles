"""Guarded post-processing cleanup; this module never performs recovery."""

from __future__ import annotations

import logging
import ntpath
import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from tidyfiles.collection import CollectionSnapshot
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsPathObserver,
    is_within,
    path_key,
)
from tidyfiles.filesystem_paths import CLUTTER_FILENAMES, is_protected_system_path
from tidyfiles.modifying_jobs import ModifyingJobCoordinator
from tidyfiles.recovery import RecoveryConflict, RecoveryGuard
from tidyfiles.storage import managed_trash_path
from tidyfiles.transaction_journal import JournalSession, JournalStore
from tidyfiles.transaction_record import EntryOutcome, OperationType, TransactionState
from tidyfiles.windows_cleanup import directory_entries, remove_empty_directory, remove_file

_LOG = logging.getLogger(__name__)
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_TRASH_BATCH_NAME = re.compile(
    r"(?:[0-9]{8}_[0-9]{6}|[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32})", re.I
)


class CleanupStatus(StrEnum):
    ELIGIBLE = "eligible"
    DELETED = "deleted"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CleanupItem:
    path: str
    status: CleanupStatus
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupResult:
    items: tuple[CleanupItem, ...] = ()
    detail: str | None = None

    @property
    def deleted_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.items if item.status is CleanupStatus.DELETED)

    @property
    def failures(self) -> tuple[CleanupItem, ...]:
        return tuple(item for item in self.items if item.status is CleanupStatus.FAILED)


@dataclass(frozen=True, slots=True)
class AutoCleanRequest:
    snapshot: CollectionSnapshot
    enabled: bool
    primary_succeeded: bool
    partial: bool = False
    unresolved: bool = False
    owning_job_id: UUID | None = None
    output_paths: tuple[str, ...] = ()


class AutoCleanService:
    """Delete only allowlisted files and empty descendants of explicit Add Folder roots."""

    def __init__(
        self,
        coordinator: ModifyingJobCoordinator,
        journals: JournalStore,
        *,
        managed_paths: tuple[str, ...] = (),
    ) -> None:
        self.coordinator = coordinator
        self.journals = journals
        trash = managed_trash_path()
        self.managed_paths = tuple(
            dict.fromkeys(
                (*journals.managed_paths, *managed_paths, *((str(trash),) if trash else ()))
            )
        )

    def run(self, request: AutoCleanRequest) -> CleanupResult:
        reason = self._ineligible(request)
        if reason is not None:
            return CleanupResult(detail=reason)
        if os.name != "nt":
            return CleanupResult(detail="Windows identity-checked cleanup is unavailable")
        owns_job = request.owning_job_id is not None
        job_id = request.owning_job_id
        if owns_job:
            current = self.coordinator.current
            if current is None or current.job_id != job_id or not current.modifying:
                return CleanupResult(detail="The active modifying job cannot authorize Auto-clean")
        else:
            try:
                job_id = self.coordinator.reserve(OperationType.CLEANUP).job_id
            except Exception as error:
                return CleanupResult(
                    detail=f"Auto-clean could not reserve the cleanup job: {error}"
                )
        try:
            try:
                return self._clean_roots(request.snapshot, request.output_paths)
            except Exception as error:
                result = CleanupResult(detail=f"Auto-clean failed: {error}")
                _log_cleanup("Auto-clean", result)
                return result
        finally:
            if not owns_job and job_id is not None:
                self.coordinator.finish(job_id)

    @staticmethod
    def _ineligible(request: AutoCleanRequest) -> str | None:
        snapshot = request.snapshot
        if not request.enabled:
            return "Auto-clean is disabled"
        if not request.primary_succeeded or request.partial or request.unresolved:
            return "Auto-clean requires complete primary-operation success"
        if snapshot.capabilities.is_importing or snapshot.has_incomplete_imports:
            return "Auto-clean is unavailable after incomplete folder discovery"
        if any(not entry.is_finalized for entry in snapshot.entries):
            return "Auto-clean requires finalized collection entries"
        if not snapshot.folder_roots:
            return "The active snapshot has no explicit Add Folder roots"
        if snapshot.entries and not request.output_paths:
            return "Auto-clean requires the completed operation's output paths"
        return None

    def _clean_roots(
        self, snapshot: CollectionSnapshot, output_paths: tuple[str, ...]
    ) -> CleanupResult:
        observer = WindowsPathObserver(self.managed_paths, capture_identity=True)
        root_values = {}
        for root in snapshot.folder_roots:
            root_values.setdefault(path_key(root.path), root.path)
        root_paths = tuple(root_values.values())
        root_keys = {path_key(path) for path in root_paths}
        output_keys = {path_key(path) for path in output_paths}
        protected_keys = tuple(path_key(path) for path in self.managed_paths)
        directories: dict[str, tuple[str, PathObservation]] = {}
        files: dict[str, tuple[str, PathObservation]] = {}
        items: list[CleanupItem] = []
        root_volumes: dict[str, str | None] = {}
        stack: list[tuple[str, str, PathObservation, bool]] = []

        for root in root_paths:
            key = path_key(root)
            if self._protected(root, protected_keys):
                items.append(CleanupItem(root, CleanupStatus.SKIPPED, "protected_root"))
                continue
            observed = observer.observe_directory(root)
            if (
                observed.occupancy is not PathOccupancy.DIRECTORY
                or observed.object_metadata is None
            ):
                items.append(
                    CleanupItem(root, CleanupStatus.SKIPPED, observed.reason or "root_unverifiable")
                )
                continue
            root_volumes[key] = observed.volume_guid
            stack.append((root, key, observed, True))

        while stack:
            directory, root_key, initial, is_root = stack.pop()
            if self._protected(directory, protected_keys):
                continue
            current = observer.observe_directory(directory)
            if not _same_directory(initial, current):
                items.append(CleanupItem(directory, CleanupStatus.SKIPPED, "directory_changed"))
                continue
            assert initial.object_metadata is not None
            scanned = True
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path = entry.path
                        name = entry.name
                        path_id = path_key(path)
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except OSError as error:
                            if name.casefold() in CLUTTER_FILENAMES:
                                items.append(CleanupItem(path, CleanupStatus.FAILED, str(error)))
                            continue
                        attributes = getattr(info, "st_file_attributes", 0)
                        if (
                            stat.S_ISLNK(info.st_mode)
                            or attributes & _REPARSE
                            or is_protected_system_path(path)
                            or self._inside_managed(path, protected_keys)
                        ):
                            if name.casefold() in CLUTTER_FILENAMES:
                                items.append(
                                    CleanupItem(path, CleanupStatus.SKIPPED, "unsafe_entry")
                                )
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            child = observer.observe_directory(path)
                            if (
                                child.occupancy is not PathOccupancy.DIRECTORY
                                or child.volume_guid != root_volumes[root_key]
                                or child.parent_identity != initial.object_metadata.identity
                                or child.object_metadata is None
                            ):
                                items.append(
                                    CleanupItem(path, CleanupStatus.SKIPPED, "unsafe_directory")
                                )
                                continue
                            directories[path_id] = (path, child)
                            stack.append((path, root_key, child, False))
                        elif name.casefold() in CLUTTER_FILENAMES:
                            if path_id in output_keys:
                                items.append(
                                    CleanupItem(path, CleanupStatus.SKIPPED, "primary_output")
                                )
                                continue
                            observed_file = observer(path)
                            if (
                                observed_file.occupancy is PathOccupancy.FILE
                                and observed_file.volume_guid == root_volumes[root_key]
                                and observed_file.parent_identity
                                == initial.object_metadata.identity
                                and observed_file.object_metadata is not None
                            ):
                                files[path_id] = (path, observed_file)
                            else:
                                items.append(
                                    CleanupItem(
                                        path,
                                        CleanupStatus.SKIPPED,
                                        observed_file.reason or "file_unverifiable",
                                    )
                                )
            except OSError as error:
                scanned = False
                items.append(CleanupItem(directory, CleanupStatus.FAILED, str(error)))
            if not is_root and scanned:
                directories.setdefault(path_key(directory), (directory, initial))

        for path, expected in files.values():
            items.append(self._delete_file(path, expected))
        for key, (path, expected) in sorted(
            directories.items(),
            key=lambda item: (item[0].count("\\"), item[0]),
            reverse=True,
        ):
            if key in root_keys:
                continue
            try:
                with os.scandir(path) as current:
                    if next(current, None) is not None:
                        items.append(
                            CleanupItem(path, CleanupStatus.SKIPPED, "directory_not_empty")
                        )
                        continue
            except OSError as error:
                items.append(CleanupItem(path, CleanupStatus.FAILED, str(error)))
                continue
            fresh = WindowsPathObserver(self.managed_paths, capture_identity=True)
            current = fresh.observe_directory(path)
            if not _same_directory(expected, current):
                items.append(CleanupItem(path, CleanupStatus.SKIPPED, "directory_changed"))
                continue
            try:
                RecoveryGuard(self.journals).check((path,), (current,))
                remove_empty_directory(fresh, path, current)
                items.append(CleanupItem(path, CleanupStatus.DELETED))
            except RecoveryConflict as error:
                items.append(CleanupItem(path, CleanupStatus.SKIPPED, str(error)))
            except OSError as error:
                if getattr(error, "winerror", None) == 145 or error.errno in {39, 66}:
                    items.append(CleanupItem(path, CleanupStatus.SKIPPED, "directory_not_empty"))
                elif str(error) == "The directory changed after cleanup inspection":
                    items.append(CleanupItem(path, CleanupStatus.SKIPPED, "directory_changed"))
                else:
                    items.append(CleanupItem(path, CleanupStatus.FAILED, str(error)))
            except Exception as error:
                items.append(CleanupItem(path, CleanupStatus.FAILED, str(error)))
        result = CleanupResult(tuple(items))
        _log_cleanup("Auto-clean", result)
        return result

    def _delete_file(self, path: str, expected: PathObservation) -> CleanupItem:
        fresh = WindowsPathObserver(self.managed_paths, capture_identity=True)
        current = fresh(path)
        if current != expected:
            return CleanupItem(path, CleanupStatus.SKIPPED, "file_changed")
        try:
            RecoveryGuard(self.journals).check((path,), (current,))
            remove_file(fresh, path, current)
            return CleanupItem(path, CleanupStatus.DELETED)
        except RecoveryConflict as error:
            return CleanupItem(path, CleanupStatus.SKIPPED, str(error))
        except OSError as error:
            return CleanupItem(path, CleanupStatus.FAILED, str(error))
        except Exception as error:
            return CleanupItem(path, CleanupStatus.FAILED, str(error))

    def _protected(self, path: str, protected_keys: tuple[str, ...]) -> bool:
        return is_protected_system_path(path) or self._inside_managed(path, protected_keys)

    @staticmethod
    def _inside_managed(path: str, protected_keys: tuple[str, ...]) -> bool:
        key = path_key(path)
        return any(is_within(key, root) for root in protected_keys)


@dataclass(frozen=True, slots=True)
class TemporaryArtifact:
    """Creation-time identity retained by the owning operation until cleanup."""

    entry_id: UUID
    path: str
    observation: PathObservation


class TemporaryArtifactCleaner:
    """Remove only an identity-bound temporary output owned by the active journal."""

    def __init__(self, coordinator: ModifyingJobCoordinator, journals: JournalStore) -> None:
        self.coordinator = coordinator
        self.journals = journals
        self.managed_paths = journals.managed_paths

    def clean_unneeded(
        self,
        job_id: UUID,
        journal: JournalSession,
        artifacts: tuple[TemporaryArtifact, ...],
    ) -> CleanupResult:
        current = self.coordinator.current
        if (
            current is None
            or current.job_id != job_id
            or not current.modifying
            or not journal.can_modify
            or journal.record.state is not TransactionState.IN_PROGRESS
        ):
            return CleanupResult(
                detail="Temporary cleanup requires its active modifying transaction"
            )
        if os.name != "nt":
            return CleanupResult(detail="Windows identity-checked cleanup is unavailable")
        records = {entry.entry_id: entry for entry in journal.record.entries}
        all_source_ids = {
            (
                observation.volume_guid,
                observation.object_metadata.identity,
            )
            for entry in journal.record.entries
            for observation in (entry.source_observation, entry.destination_observation)
            if observation is not None and observation.object_metadata is not None
        }
        all_owned_paths = {
            path_key(path)
            for entry in journal.record.entries
            for path in (entry.source_path, entry.destination_path)
        }
        items = []
        ordered_artifacts = sorted(
            artifacts,
            key=lambda item: (
                item.observation.occupancy is PathOccupancy.DIRECTORY,
                -item.path.count("\\")
                if item.observation.occupancy is PathOccupancy.DIRECTORY
                else 0,
            ),
        )
        for artifact in ordered_artifacts:
            entry = records.get(artifact.entry_id)
            expected = artifact.observation
            if (
                entry is None
                or entry.temporary_path is None
                or entry.outcome is not EntryOutcome.SUCCEEDED
                or path_key(artifact.path) != path_key(entry.temporary_path)
                or path_key(artifact.path) in all_owned_paths
                or is_protected_system_path(artifact.path)
                or expected.occupancy not in {PathOccupancy.FILE, PathOccupancy.DIRECTORY}
                or expected.object_metadata is None
                or (expected.volume_guid, expected.object_metadata.identity) in all_source_ids
            ):
                items.append(
                    CleanupItem(
                        artifact.path, CleanupStatus.SKIPPED, "artifact_ownership_unverified"
                    )
                )
                continue
            observer = WindowsPathObserver(self.managed_paths, capture_identity=True)
            current_path = (
                observer.observe_directory(artifact.path)
                if expected.occupancy is PathOccupancy.DIRECTORY
                else observer(artifact.path)
            )
            if current_path != expected:
                items.append(CleanupItem(artifact.path, CleanupStatus.SKIPPED, "artifact_changed"))
                continue
            try:
                RecoveryGuard(self.journals).check(
                    (artifact.path,),
                    (current_path,),
                    ignore_transaction_id=journal.record.transaction_id,
                )
                if current_path.occupancy is PathOccupancy.DIRECTORY:
                    with os.scandir(artifact.path) as children:
                        if next(children, None) is not None:
                            items.append(
                                CleanupItem(
                                    artifact.path,
                                    CleanupStatus.SKIPPED,
                                    "directory_not_empty",
                                )
                            )
                            continue
                    remove_empty_directory(observer, artifact.path, current_path)
                else:
                    remove_file(observer, artifact.path, current_path)
                items.append(CleanupItem(artifact.path, CleanupStatus.DELETED))
            except RecoveryConflict as error:
                items.append(CleanupItem(artifact.path, CleanupStatus.SKIPPED, str(error)))
            except OSError as error:
                if getattr(error, "winerror", None) == 145 or error.errno in {39, 66}:
                    items.append(
                        CleanupItem(artifact.path, CleanupStatus.SKIPPED, "directory_not_empty")
                    )
                else:
                    items.append(CleanupItem(artifact.path, CleanupStatus.FAILED, str(error)))
            except Exception as error:
                items.append(CleanupItem(artifact.path, CleanupStatus.FAILED, str(error)))
        result = CleanupResult(tuple(items))
        _log_cleanup("Temporary artifact cleanup", result)
        return result


@dataclass(frozen=True, slots=True)
class TrashInspectionSummary:
    eligible_count: int
    eligible_size_bytes: int
    items: tuple[CleanupItem, ...]
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TrashClearResult:
    inspection: TrashInspectionSummary
    cleanup: CleanupResult


@dataclass(frozen=True, slots=True)
class _TrashCandidate:
    path: str
    observation: PathObservation
    is_directory: bool


class TrashClearService:
    """Clear contents only within recognized application-managed Trash batches."""

    def __init__(self, coordinator: ModifyingJobCoordinator, journals: JournalStore) -> None:
        self.coordinator = coordinator
        self.journals = journals

    def inspect(self) -> TrashInspectionSummary:
        summary, _ = self._inspect()
        return summary

    def _inspect(self) -> tuple[TrashInspectionSummary, tuple[_TrashCandidate, ...]]:
        root = managed_trash_path()
        if root is None or os.name != "nt":
            return (
                TrashInspectionSummary(0, 0, (), "The managed Trash location is unavailable"),
                (),
            )
        if is_protected_system_path(str(root)):
            return (
                TrashInspectionSummary(0, 0, (), "Managed Trash is inside a protected system path"),
                (),
            )
        observer = WindowsPathObserver(capture_identity=True, allow_managed_trash=True)
        root_observation = observer.observe_directory(str(root))
        if root_observation.occupancy is not PathOccupancy.DIRECTORY:
            if root_observation.occupancy is PathOccupancy.VACANT:
                return TrashInspectionSummary(0, 0, (), "Managed Trash does not exist"), ()
            return (
                TrashInspectionSummary(
                    0, 0, (), root_observation.reason or "Managed Trash is inaccessible or unsafe"
                ),
                (),
            )
        assert root_observation.object_metadata is not None
        items = []
        files: list[_TrashCandidate] = []
        directories: dict[str, _TrashCandidate] = {}
        recovery = self.journals.inspect_recovery()
        guard = RecoveryGuard(self.journals)
        root_key = path_key(str(root))
        try:
            root_entries = directory_entries(observer, str(root), root_observation)
        except OSError as error:
            return TrashInspectionSummary(0, 0, (), str(error)), ()
        if not _same_directory(root_observation, observer.observe_directory(str(root))):
            return (
                TrashInspectionSummary(
                    0, 0, (CleanupItem(str(root), CleanupStatus.SKIPPED, "directory_changed"),)
                ),
                (),
            )

        stack = []
        for name, attributes in root_entries:
            path = ntpath.join(str(root), name)
            if not is_within(path_key(path), root_key):
                items.append(CleanupItem(path, CleanupStatus.SKIPPED, "outside_trash_boundary"))
                continue
            if attributes & _REPARSE:
                items.append(CleanupItem(path, CleanupStatus.SKIPPED, "linked_entry"))
                continue
            if (
                not attributes & stat.FILE_ATTRIBUTE_DIRECTORY
                or not _TRASH_BATCH_NAME.fullmatch(name)
            ):
                items.append(
                    CleanupItem(path, CleanupStatus.SKIPPED, "outside_recognized_batch")
                )
                continue
            observed = observer.observe_directory(path)
            if (
                observed.occupancy is not PathOccupancy.DIRECTORY
                or observed.parent_identity != root_observation.object_metadata.identity
                or observed.volume_guid != root_observation.volume_guid
            ):
                items.append(CleanupItem(path, CleanupStatus.SKIPPED, "unsafe_batch"))
                continue
            directories[path_key(path)] = _TrashCandidate(path, observed, True)
            stack.append((path, observed))

        while stack:
            directory, expected_directory = stack.pop()
            current_directory = observer.observe_directory(directory)
            if not _same_directory(expected_directory, current_directory):
                items.append(CleanupItem(directory, CleanupStatus.SKIPPED, "directory_changed"))
                continue
            assert current_directory.object_metadata is not None
            try:
                children = directory_entries(observer, directory, current_directory)
            except OSError as error:
                items.append(CleanupItem(directory, CleanupStatus.FAILED, str(error)))
                continue
            if not _same_directory(expected_directory, observer.observe_directory(directory)):
                items.append(CleanupItem(directory, CleanupStatus.SKIPPED, "directory_changed"))
                continue
            for name, attributes in children:
                path = ntpath.join(directory, name)
                if not is_within(path_key(path), root_key):
                    items.append(CleanupItem(path, CleanupStatus.SKIPPED, "outside_trash_boundary"))
                    continue
                if attributes & _REPARSE:
                    items.append(CleanupItem(path, CleanupStatus.SKIPPED, "linked_entry"))
                    continue
                if is_protected_system_path(path):
                    items.append(CleanupItem(path, CleanupStatus.SKIPPED, "protected_system_path"))
                    continue
                if attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                    observed = observer.observe_directory(path)
                    if (
                        observed.occupancy is not PathOccupancy.DIRECTORY
                        or observed.parent_identity != current_directory.object_metadata.identity
                        or observed.volume_guid != root_observation.volume_guid
                    ):
                        items.append(CleanupItem(path, CleanupStatus.SKIPPED, "unsafe_directory"))
                        continue
                    directories[path_key(path)] = _TrashCandidate(path, observed, True)
                    stack.append((path, observed))
                    continue
                observed = observer(path)
                if (
                    observed.occupancy is not PathOccupancy.FILE
                    or observed.parent_identity != current_directory.object_metadata.identity
                    or observed.volume_guid != root_observation.volume_guid
                    or observed.object_metadata is None
                ):
                    items.append(
                        CleanupItem(path, CleanupStatus.SKIPPED, observed.reason or "unsafe_file")
                    )
                    continue
                candidate = _TrashCandidate(path, observed, False)
                try:
                    guard.check((path,), (observed,), inspection=recovery)
                except RecoveryConflict:
                    reason = (
                        "recovery_scope_unverifiable"
                        if recovery.has_incomplete_discovery()
                        else "recovery_dependent"
                    )
                    items.append(CleanupItem(path, CleanupStatus.SKIPPED, reason))
                    continue
                files.append(candidate)
                items.append(CleanupItem(path, CleanupStatus.ELIGIBLE))

        candidates = [*files, *directories.values()]
        for candidate in directories.values():
            try:
                guard.check(
                    (candidate.path,),
                    (candidate.observation,),
                    inspection=recovery,
                )
            except RecoveryConflict:
                reason = (
                    "recovery_scope_unverifiable"
                    if recovery.has_incomplete_discovery()
                    else "recovery_dependent"
                )
                items.append(CleanupItem(candidate.path, CleanupStatus.SKIPPED, reason))
                candidates.remove(candidate)
        summary = TrashInspectionSummary(
            len(files),
            sum(item.observation.object_metadata.size_bytes for item in files),
            tuple(items),
        )
        ordered = tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.is_directory,
                    -item.path.count("\\") if item.is_directory else 0,
                ),
            )
        )
        return summary, ordered

    def clear(
        self,
        *,
        manually_authorized: bool = False,
        auto_clear_enabled: bool = False,
        normal_exit: bool = False,
    ) -> TrashClearResult:
        inspection, candidates = self._inspect()
        if normal_exit:
            if not auto_clear_enabled:
                return TrashClearResult(
                    inspection, CleanupResult(detail="Auto-Clear Trash is disabled")
                )
            if self.coordinator.busy:
                return TrashClearResult(
                    inspection,
                    CleanupResult(detail="Trash cannot be cleared during another modifying job"),
                )
        elif not manually_authorized:
            return TrashClearResult(
                inspection,
                CleanupResult(detail="Explicit authorization is required to clear Trash"),
            )
        if inspection.detail:
            return TrashClearResult(inspection, CleanupResult(detail=inspection.detail))
        if not candidates:
            return TrashClearResult(inspection, CleanupResult(detail="No eligible Trash items"))
        try:
            job_id = self.coordinator.reserve(OperationType.CLEANUP).job_id
        except Exception as error:
            return TrashClearResult(
                inspection,
                CleanupResult(detail=f"Trash clear could not reserve a cleanup job: {error}"),
            )
        try:
            root = managed_trash_path()
            if root is None:
                return TrashClearResult(
                    inspection, CleanupResult(detail="The managed Trash location is unavailable")
                )
            observer = WindowsPathObserver(capture_identity=True, allow_managed_trash=True)
            items = []
            guard = RecoveryGuard(self.journals)
            for candidate in candidates:
                if not is_within(path_key(candidate.path), path_key(str(root))):
                    items.append(
                        CleanupItem(candidate.path, CleanupStatus.SKIPPED, "outside_trash_boundary")
                    )
                    continue
                try:
                    current = (
                        observer.observe_directory(candidate.path)
                        if candidate.is_directory
                        else observer(candidate.path)
                    )
                    matches = (
                        _same_directory(candidate.observation, current)
                        if candidate.is_directory
                        else current == candidate.observation
                    )
                    if not matches:
                        items.append(
                            CleanupItem(candidate.path, CleanupStatus.SKIPPED, "entry_changed")
                        )
                        continue
                    guard.check((candidate.path,), (current,))
                    if candidate.is_directory:
                        if directory_entries(observer, candidate.path, current):
                            items.append(
                                CleanupItem(
                                    candidate.path,
                                    CleanupStatus.SKIPPED,
                                    "directory_not_empty",
                                )
                            )
                            continue
                        remove_empty_directory(observer, candidate.path, current)
                    else:
                        remove_file(observer, candidate.path, current)
                    items.append(CleanupItem(candidate.path, CleanupStatus.DELETED))
                except RecoveryConflict as error:
                    items.append(
                        CleanupItem(candidate.path, CleanupStatus.SKIPPED, str(error))
                    )
                except OSError as error:
                    if candidate.is_directory and (
                        getattr(error, "winerror", None) == 145 or error.errno in {39, 66}
                    ):
                        items.append(
                            CleanupItem(
                                candidate.path,
                                CleanupStatus.SKIPPED,
                                "directory_not_empty",
                            )
                        )
                    else:
                        items.append(CleanupItem(candidate.path, CleanupStatus.FAILED, str(error)))
                except Exception as error:
                    items.append(CleanupItem(candidate.path, CleanupStatus.FAILED, str(error)))
            result = CleanupResult(tuple(items))
            _log_cleanup("Trash clear", result)
            return TrashClearResult(inspection, result)
        finally:
            self.coordinator.finish(job_id)


def _same_directory(expected: PathObservation, current: PathObservation) -> bool:
    return bool(
        expected.occupancy is PathOccupancy.DIRECTORY
        and current.occupancy is PathOccupancy.DIRECTORY
        and expected.object_metadata is not None
        and current.object_metadata is not None
        and expected.object_metadata.identity == current.object_metadata.identity
        and expected.volume_guid == current.volume_guid
        and expected.parent_identity == current.parent_identity
        and expected.actual_parent == current.actual_parent
        and expected.actual_name == current.actual_name
    )


def _log_cleanup(label: str, result: CleanupResult) -> None:
    try:
        for item in result.items:
            getattr(_LOG, "error" if item.status is CleanupStatus.FAILED else "info")(
                "%s %s: %s (%s)", label, item.status, item.path, item.reason or ""
            )
        if result.detail:
            _LOG.info("%s: %s", label, result.detail)
    except Exception:
        pass
