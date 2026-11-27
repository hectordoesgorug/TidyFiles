"""Standalone recoverable deletion into the application's managed Trash."""

from __future__ import annotations

import logging
import ntpath
import os
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from tidyfiles.collection import CollectionSnapshot, FileSystemIdentitySnapshot
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsObjectObservation,
    WindowsPathObserver,
    path_key,
)
from tidyfiles.destination_planning import DestinationOptions, planning_observer
from tidyfiles.duplicate_analysis import _observe, hold_exact_duplicate
from tidyfiles.modifying_jobs import ModifyingJobCoordinator
from tidyfiles.recovery import RecoveryGuard
from tidyfiles.rename_executor import _expected_file, _parents_match, _retain
from tidyfiles.storage import managed_trash_path
from tidyfiles.transaction_journal import JournalSession, JournalStore
from tidyfiles.transaction_record import (
    TRASH_COPY_BEFORE_REMOVE,
    TRASH_COPY_REMOVED,
    EntryOutcome,
    EntryVerification,
    OperationType,
    TransactionEntry,
    TransactionRecord,
    VerifiedOutcome,
    validate_verification,
)
from tidyfiles.windows_rename import bind_rename
from tidyfiles.windows_trash import copy_across_volumes, create_batch

_LOG = logging.getLogger(__name__)


def _file_key(observation: PathObservation) -> tuple[str | None, FileSystemIdentitySnapshot]:
    assert observation.object_metadata is not None
    return observation.volume_guid, observation.object_metadata.identity


class TrashState(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED_SAFE = "failed_safe"
    RECOVERY_REQUIRED = "recovery_required"


class TrashOutcome(StrEnum):
    TRASHED = "moved_to_trash"
    FAILED_SAFE = "failed_original_verified"
    NOT_PROCESSED = "not_processed"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class TrashKeeper:
    path: str
    observation: PathObservation


@dataclass(frozen=True, slots=True)
class TrashSource:
    entry_id: UUID
    path: str
    observation: PathObservation
    keeper: TrashKeeper | None = None


@dataclass(frozen=True, slots=True)
class TrashScope:
    """The exact ordered set a caller has explicitly authorized."""

    sources: tuple[TrashSource, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True, slots=True)
class TrashEntryResult:
    entry_id: UUID
    source_path: str
    outcome: TrashOutcome
    destination_path: str | None = None
    trash_path: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TrashResult:
    state: TrashState
    transaction_id: UUID | None
    entries: tuple[TrashEntryResult, ...]
    detail: str | None = None


class TrashExecutor:
    """Run an explicitly authorized Trash scope off the GUI thread."""

    def __init__(self, coordinator: ModifyingJobCoordinator, journals: JournalStore) -> None:
        self.coordinator = coordinator
        self.journals = journals

    def execute_duplicates(self, authorized: CollectionSnapshot) -> TrashResult:
        """Transfer only the excluded paths authorized by the Deduplicate action."""
        observer = WindowsPathObserver(self.journals.managed_paths, capture_identity=True)
        active = {entry.entry_id: entry for entry in authorized.entries}
        sources = []
        failed = []
        for duplicate in authorized.excluded_duplicates:
            entry = duplicate.entry
            try:
                keeper = active.get(duplicate.keeper.entry_id)
                if keeper != duplicate.keeper or keeper is None:
                    raise OSError("The designated keeper is no longer in the processing collection")
                source = _observe(observer, entry).observation
                if source.read_only_volume:
                    raise OSError("The duplicate is on a read-only volume")
                retained = _observe(observer, keeper).observation
            except OSError as error:
                failed.append(TrashEntryResult(
                    entry.entry_id, entry.source_path, TrashOutcome.NOT_PROCESSED, detail=str(error)
                ))
            else:
                sources.append(TrashSource(
                    entry.entry_id, entry.source_path, source,
                    TrashKeeper(keeper.source_path, retained),
                ))

        if not sources:
            return TrashResult(TrashState.FAILED_SAFE, None, tuple(failed))
        result = self.execute(authorized=TrashScope(tuple(sources)))
        if not failed:
            return result
        outcomes = {entry.entry_id: entry for entry in (*result.entries, *failed)}
        return replace(
            result,
            state=TrashState.FAILED_SAFE if result.state is TrashState.SUCCEEDED else result.state,
            entries=tuple(outcomes[row.entry.entry_id] for row in authorized.excluded_duplicates),
        )

    def execute(self, *, authorized: TrashScope | None) -> TrashResult:
        result = self._execute(authorized)
        for entry in result.entries:
            _LOG.info(
                "Trash transaction %s: %s -> %s, actual=%s, outcome=%s, detail=%s",
                result.transaction_id,
                entry.source_path,
                entry.destination_path,
                entry.trash_path,
                entry.outcome,
                entry.detail or result.detail,
            )
        if not result.entries:
            _LOG.info("Trash request %s: %s", result.state, result.detail)
        return result

    def _execute(self, scope: TrashScope | None) -> TrashResult:
        if os.name != "nt" or scope is None or not scope.sources:
            return TrashResult(TrashState.REJECTED, None, (), "A Windows Trash scope is required")
        pending = tuple(
            TrashEntryResult(item.entry_id, item.path, TrashOutcome.NOT_PROCESSED)
            for item in scope.sources
        )
        try:
            job = self.coordinator.reserve(OperationType.TRASH)
        except Exception as error:
            return TrashResult(TrashState.REJECTED, None, pending, str(error))
        journal = None
        identifier = None
        try:
            root = managed_trash_path()
            if root is None:
                raise OSError("The application directory is unavailable for managed Trash")
            run = _TrashRun(self.journals.managed_paths)
            run.check_scope(scope)
            RecoveryGuard(self.journals).check(
                tuple(item.path for item in scope.sources),
                tuple(item.observation for item in scope.sources),
            )
            name = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex}"
            with create_batch(root, name) as batch:
                entries = run.destinations(batch, scope)
                pending = tuple(
                    replace(item, destination_path=row.destination_path)
                    for item, row in zip(pending, entries, strict=True)
                )
                record = TransactionRecord.prepared(OperationType.TRASH, entries)
                identifier = record.transaction_id
                journal = self.journals.prepare(record)
                self.coordinator.begin_modification(job.job_id, journal)
                RecoveryGuard(self.journals).check(
                    tuple(item.path for item in scope.sources)
                    + tuple(entry.destination_path for entry in entries),
                    tuple(item.observation for item in scope.sources)
                    + tuple(
                        entry.destination_observation
                        for entry in entries
                        if entry.destination_observation is not None
                    ),
                    ignore_transaction_id=journal.record.transaction_id,
                )
                return run.execute(journal, scope)
        except Exception as error:
            detail = str(error)
            active = self.coordinator.current
            if journal is not None and active is not None and active.modifying:
                detail = _retain(journal, detail)
                return TrashResult(
                    TrashState.RECOVERY_REQUIRED,
                    identifier,
                    tuple(
                        replace(item, outcome=TrashOutcome.RECOVERY_REQUIRED) for item in pending
                    ),
                    detail,
                )
            return TrashResult(TrashState.FAILED_SAFE, identifier, pending, detail)
        finally:
            if journal is not None and journal.can_modify:
                _retain(journal, "Trash stopped before verification")
            self.coordinator.finish(job.job_id)


class _TrashRun:
    def __init__(self, managed_paths: tuple[str, ...]) -> None:
        self.options = DestinationOptions(managed_paths=managed_paths)
        self.metadata: dict[
            tuple[str | None, FileSystemIdentitySnapshot], WindowsObjectObservation
        ] = {}
        self.completed: dict[UUID, EntryVerification] = {}

    def source_observer(self):
        native = WindowsPathObserver(self.options.managed_paths, capture_identity=True)
        return planning_observer(self.options, native)

    @staticmethod
    def target_observer() -> WindowsPathObserver:
        return WindowsPathObserver(capture_identity=True, allow_managed_trash=True)

    def check_scope(self, scope: TrashScope) -> None:
        observe = self.source_observer()
        ids, paths = set(), set()
        source_paths = {path_key(item.path) for item in scope.sources}
        for item in scope.sources:
            current = observe(item.path)
            if not _expected_file(current, item.observation) or current.read_only_volume:
                raise OSError(
                    f"Authorized Trash source changed or is unsafe: {item.path} ({current.reason})"
                )
            assert current.actual_name is not None
            key = current.volume_guid, current.parent_identity, path_key(current.actual_name)
            if item.entry_id in ids or key in paths:
                raise ValueError("The Trash scope repeats a collection ID or directory entry")
            if item.keeper is not None and path_key(item.keeper.path) in source_paths:
                raise ValueError("A retained keeper is in the Trash scope")
            ids.add(item.entry_id)
            paths.add(key)

    def destinations(self, batch: Path, scope: TrashScope) -> tuple[TransactionEntry, ...]:
        observe = self.target_observer()
        names = set()
        for item in scope.sources:
            original = item.observation.actual_name
            assert original is not None
            names.add(path_key(original))
        used = set()
        entries = []
        required_bytes = 0
        cross_volume = False
        for item in scope.sources:
            original = item.observation.actual_name
            assert original is not None and item.observation.object_metadata is not None
            name = original
            for attempt in range(8):
                destination = str(batch / name)
                target = observe(destination)
                key = path_key(name)
                if (
                    target.occupancy is PathOccupancy.VACANT
                    and key not in used
                    and (name == original or key not in names)
                ):
                    break
                if target.occupancy in (PathOccupancy.UNSAFE, PathOccupancy.UNVERIFIABLE) and (
                    target.reason not in {"unsupported_long_path", "volume_component_limit"}
                ):
                    raise OSError(
                        f"Trash destination cannot be verified: {destination} ({target.reason})"
                    )
                suffix = ntpath.splitext(original)[1] if attempt == 0 else ".trash"
                name = f"{uuid4().hex}{suffix}"
            else:
                raise OSError(f"No safe Trash destination for {item.path}")
            if (
                target.read_only_volume
                or not target.preserves_case
                or target.parent_identity is None
            ):
                raise OSError(f"Trash destination cannot preserve the file: {destination}")
            if item.observation.volume_guid != target.volume_guid:
                cross_volume = True
                required_bytes += item.observation.object_metadata.size_bytes
            used.add(key)
            entries.append(
                TransactionEntry(item.entry_id, item.path, destination, item.observation, target)
            )
        if cross_volume and shutil.disk_usage(batch).free < required_bytes:
            raise OSError("Insufficient space in managed Trash for the cross-volume copies")
        return tuple(entries)

    def _current(self, observation: PathObservation) -> PathObservation:
        metadata = self.metadata.get(_file_key(observation), observation.object_metadata)
        return replace(observation, object_metadata=metadata)

    def execute(self, journal: JournalSession, scope: TrashScope) -> TrashResult:
        failed_entry = None
        detail = None
        entries = journal.record.entries
        last = {}
        surviving = {}
        for row in reversed(entries):
            key = _file_key(row.source_observation)
            if key in last:
                surviving[row.entry_id] = last[key]
            last[key] = row
        for index, (row, item) in enumerate(zip(entries, scope.sources, strict=True)):
            try:
                self._transfer(journal, row, surviving.get(row.entry_id), item.keeper)
            except Exception as error:
                failed_entry = index
                detail = str(error)
                break
        return self._finish(journal, failed_entry, detail)

    def _transfer(
        self, journal: JournalSession, row: TransactionEntry, sibling: TransactionEntry | None,
        keeper: TrashKeeper | None,
    ) -> None:
        source, target = self._current(row.source_observation), row.destination_observation
        assert target is not None and source.object_metadata is not None
        observe = self.target_observer()
        if not _expected_file(self.source_observer()(row.source_path), source):
            raise OSError(f"Authorized Trash source changed: {row.source_path}")
        if observe(row.destination_path) != target:
            raise OSError(f"Trash destination changed: {row.destination_path}")
        with (
            hold_exact_duplicate(
                observe, row.source_path, source, keeper.path, self._current(keeper.observation)
            )
            if keeper is not None else nullcontext()
        ):
            self._move(journal, row, sibling, source, target, observe, keeper)

    def _move(
        self, journal: JournalSession, row: TransactionEntry, sibling: TransactionEntry | None,
        source: PathObservation, target: PathObservation, observe: WindowsPathObserver,
        keeper: TrashKeeper | None,
    ) -> None:
        assert source.object_metadata is not None

        def revalidate_keeper() -> None:
            if keeper is not None and self.source_observer()(keeper.path) != self._current(
                keeper.observation
            ):
                raise OSError(f"Designated keeper changed: {keeper.path}")

        identity_key = _file_key(source)
        if source.volume_guid == target.volume_guid:
            with bind_rename(
                observe, row.source_path, source, row.destination_path, target
            ) as bound:
                journal.checkpoint(replace(row, stage="before_trash_move"))
                if (
                    self.source_observer()(row.source_path) != source
                    or self.target_observer()(row.destination_path) != target
                ):
                    raise OSError("Source or Trash destination changed after the move checkpoint")
                revalidate_keeper()
                bound.move()
            actual = self.target_observer()(row.destination_path)
            expected = replace(
                target,
                actual_name=ntpath.basename(row.destination_path),
                object_metadata=source.object_metadata,
            )
            if not _expected_file(actual, expected, renamed=True):
                raise OSError("The moved Trash entry could not be verified")
            assert actual.object_metadata is not None
            self.metadata[identity_key] = actual.object_metadata
            proof = EntryVerification(
                row.entry_id, VerifiedOutcome.DESTINATION, row.destination_path, actual
            )
            stage = "trashed"
        else:
            verified = None

            def before_removal(actual: PathObservation, digest: str) -> None:
                nonlocal verified
                verified = EntryVerification(
                    row.entry_id,
                    VerifiedOutcome.TRASH_COPY,
                    row.destination_path,
                    actual,
                    content_sha256=digest,
                )
                journal.checkpoint(
                    replace(row, stage=TRASH_COPY_BEFORE_REMOVE, verification=verified)
                )
                revalidate_keeper()

            journal.checkpoint(replace(row, stage="before_trash_copy"))
            surviving_link = (
                (sibling.source_path, self._current(sibling.source_observation))
                if sibling is not None
                else None
            )
            remaining = copy_across_volumes(
                observe,
                row.source_path,
                source,
                row.destination_path,
                target,
                before_removal,
                surviving_link=surviving_link,
            )
            if remaining is not None:
                self.metadata[identity_key] = remaining
            if (
                verified is None
                or self.target_observer()(row.destination_path) != verified.observation
            ):
                raise OSError("The verified Trash copy changed after source removal")
            proof = verified
            stage = TRASH_COPY_REMOVED

        if not self._vacant(self.source_observer()(row.source_path), source):
            raise OSError("The original directory entry did not become vacant")
        validate_verification(row, proof, OperationType.TRASH)
        self.completed[row.entry_id] = proof
        journal.checkpoint(
            replace(
                row,
                stage=stage,
                outcome=EntryOutcome.SUCCEEDED,
                verification=proof if proof.outcome is VerifiedOutcome.TRASH_COPY else None,
            )
        )

    @staticmethod
    def _vacant(actual: PathObservation, expected: PathObservation) -> bool:
        return actual.occupancy is PathOccupancy.VACANT and _parents_match(actual, expected)

    def _finish(
        self, journal: JournalSession, failed_entry: int | None, detail: str | None
    ) -> TrashResult:
        record = journal.record
        source_probe, target_probe = self.source_observer(), self.target_observer()
        outcomes, evidence = [], []
        for index, row in enumerate(record.entries):
            assert row.destination_observation is not None
            original, target = source_probe(row.source_path), target_probe(row.destination_path)
            proof = self.completed.get(row.entry_id)
            status, actual = TrashOutcome.RECOVERY_REQUIRED, None
            if proof is not None:
                expected = (
                    self._current(proof.observation)
                    if proof.outcome is VerifiedOutcome.DESTINATION
                    else proof.observation
                )
                if _expected_file(target, expected) and self._vacant(
                    original, row.source_observation
                ):
                    actual = row.destination_path
                    if journal.can_modify or index != failed_entry:
                        status = TrashOutcome.TRASHED
                    evidence.append(replace(proof, observation=target))
            elif _expected_file(original, self._current(row.source_observation)) and self._vacant(
                target, row.destination_observation
            ):
                status = (
                    TrashOutcome.FAILED_SAFE
                    if index == failed_entry
                    else TrashOutcome.NOT_PROCESSED
                )
                evidence.append(
                    EntryVerification(
                        row.entry_id, VerifiedOutcome.ORIGINAL, row.source_path, original
                    )
                )
            elif row.verification is not None and target == row.verification.observation:
                actual = row.destination_path
            outcomes.append(
                TrashEntryResult(
                    row.entry_id,
                    row.source_path,
                    status,
                    row.destination_path,
                    actual,
                    detail
                    if index == failed_entry
                    else (
                        "The final source and Trash states could not both be verified"
                        if status is TrashOutcome.RECOVERY_REQUIRED
                        else None
                    ),
                )
            )
        if journal.can_modify and len(evidence) == len(outcomes):
            try:
                journal.finalize(tuple(evidence))
            except Exception as error:
                detail = f"{detail}. {error}" if detail else str(error)
            else:
                state = TrashState.SUCCEEDED if detail is None else TrashState.FAILED_SAFE
                return TrashResult(state, record.transaction_id, tuple(outcomes), detail)
        detail = _retain(
            journal, detail or "One or more Trash transfers require recovery inspection"
        )
        return TrashResult(
            TrashState.RECOVERY_REQUIRED, record.transaction_id, tuple(outcomes), detail
        )
