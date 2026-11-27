"""Sequential, journaled execution of an explicitly authorized rename plan."""

from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable
from uuid import UUID, uuid4

from tidyfiles.collection import FileSystemIdentitySnapshot
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsObjectObservation,
    WindowsPathObserver,
    path_key,
)
from tidyfiles.destination_planning import (
    DestinationEntry,
    DestinationOptions,
    DestinationPlan,
    PathObserver,
    PathOperation,
    PlanState,
    planning_observer,
    reobserve_destination_set,
)
from tidyfiles.execution_preflight import PreflightResult, PreflightState
from tidyfiles.filename_integration import EvaluationState
from tidyfiles.modifying_jobs import ModifyingJobCoordinator
from tidyfiles.recovery import RecoveryGuard
from tidyfiles.transaction_journal import JournalSession, JournalStore
from tidyfiles.transaction_record import (
    EntryOutcome,
    EntryVerification,
    OperationType,
    TransactionEntry,
    TransactionRecord,
    VerifiedOutcome,
    validate_verification,
)
from tidyfiles.windows_rename import bind_rename


class RenameState(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED_SAFE = "failed_safe"
    RECOVERY_REQUIRED = "recovery_required"


class RenameOutcome(StrEnum):
    RENAMED = "renamed"
    UNCHANGED = "unchanged"
    FAILED_SAFE = "failed_original_verified"
    NOT_PROCESSED = "not_processed"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class RenameScope:
    """The exact scope a controller must get approved independently of preflight."""

    request_id: UUID
    collection_id: UUID
    revision: int
    options: DestinationOptions
    entries: tuple[tuple[UUID, str, str], ...]
    excluded_entry_ids: tuple[UUID, ...]
    acknowledged_incomplete_import_ids: tuple[UUID, ...] = ()

    @classmethod
    def from_review(cls, result: PreflightResult) -> RenameScope:
        review = result.reviewed_plan
        included = set(result.intended_entry_ids)
        entries = tuple(
            (row.entry_id, row.source_path, _destination(row))
            for row in review.entries
            if row.entry_id in included
        )
        if not entries or tuple(row[0] for row in entries) != result.intended_entry_ids:
            raise ValueError("Reviewed scope does not match the intended entries")
        if tuple(row.entry_id for row in review.entries if row.entry_id not in included) != (
            result.excluded_entry_ids
        ):
            raise ValueError("Reviewed exclusions do not match the intended entries")
        return cls(
            result.request_id,
            review.collection_id,
            review.collection_revision,
            review.options,
            entries,
            result.excluded_entry_ids,
            result.acknowledged_incomplete_import_ids,
        )


@dataclass(frozen=True, slots=True)
class RenameProgress:
    job_id: UUID
    stage: str
    completed: int
    total: int
    entry_id: UUID | None = None
    source_path: str | None = None
    destination_path: str | None = None


@dataclass(frozen=True, slots=True)
class RenameEntryResult:
    entry_id: UUID
    outcome: RenameOutcome
    actual_path: str | None = None
    detail: str | None = None
    observation: PathObservation | None = None


@dataclass(frozen=True, slots=True)
class RenameResult:
    state: RenameState
    transaction_id: UUID | None
    entries: tuple[RenameEntryResult, ...]
    detail: str | None = None


def _destination(row: DestinationEntry) -> str:
    if row.destination_path is None:
        raise ValueError("The reviewed entry lacks a destination")
    return row.destination_path


def _parents_match(one: PathObservation, other: PathObservation) -> bool:
    return (
        one.parent_identity is not None
        and one.parent_identity == other.parent_identity
        and one.volume_key == other.volume_key
        and one.volume_guid == other.volume_guid
        and one.actual_parent == other.actual_parent
    )


def _expected_file(
    actual: PathObservation, expected: PathObservation, *, renamed: bool = False
) -> bool:
    metadata, baseline = actual.object_metadata, expected.object_metadata
    return (
        actual.occupancy is PathOccupancy.FILE
        and actual.actual_name == expected.actual_name
        and _parents_match(actual, expected)
        and metadata is not None
        and baseline is not None
        and (replace(metadata, change_time_ns=baseline.change_time_ns) if renamed else metadata)
        == baseline
    )


def _new_staging_path(source_path: str) -> str:
    return ntpath.join(ntpath.dirname(source_path), f".tidyfiles-{uuid4().hex}.tmp")


def _validate_scope(preflight: PreflightResult, authorized: RenameScope | None) -> DestinationPlan:
    plan = preflight.fresh_plan
    evaluation = preflight.fresh_evaluation
    incomplete_ids = tuple(
        session.session_id for session in preflight.incomplete_imports
    )
    if (
        preflight.state is not PreflightState.PASSED
        or preflight.acknowledged_incomplete_import_ids != incomplete_ids
        or preflight.candidate_changes
        or preflight.destination_changes
        or plan is None
        or plan.state is not PlanState.VALID
        or tuple(session.session_id for session in plan.incomplete_imports) != incomplete_ids
        or plan.acknowledged_incomplete_import_ids != incomplete_ids
        or plan.evaluation_request_id != preflight.request_id
        or plan.evaluation_settings.is_conversion
        or preflight.reviewed_plan.evaluation_settings.is_conversion
        or evaluation is None
        or evaluation.state is not EvaluationState.VALID
        or evaluation.request_id != preflight.request_id
        or (evaluation.collection_id, evaluation.collection_revision)
        != (plan.collection_id, plan.collection_revision)
        or evaluation.settings != plan.evaluation_settings
        or evaluation.expression != plan.expression
    ):
        raise ValueError("A successful current rename-only preflight is required")
    if authorized is None or authorized != RenameScope.from_review(preflight):
        raise ValueError("The reviewed scope has not been explicitly authorized")
    if (
        (plan.collection_id, plan.collection_revision, plan.options)
        != (authorized.collection_id, authorized.revision, authorized.options)
        or tuple((row.entry_id, row.source_path, _destination(row)) for row in plan.entries)
        != authorized.entries
        or plan.excluded_entry_ids != authorized.excluded_entry_ids
    ):
        raise ValueError("The execution plan differs from the authorized scope")
    return plan


class RenameExecutor:
    """The caller supplies the authorized scope and the current result authority."""

    def __init__(self, coordinator: ModifyingJobCoordinator, journals: JournalStore) -> None:
        self.coordinator = coordinator
        self.journals = journals

    def execute(
        self,
        preflight: PreflightResult,
        *,
        authorized: RenameScope | None,
        current_preflight: Callable[[], PreflightResult | None],
        progress: Callable[[RenameProgress], None] | None = None,
    ) -> RenameResult:
        not_processed = tuple(
            RenameEntryResult(identifier, RenameOutcome.NOT_PROCESSED)
            for identifier in preflight.intended_entry_ids
        )
        try:
            plan = _validate_scope(preflight, authorized)
            if os.name != "nt" or current_preflight() is not preflight:
                raise ValueError("The preflight is stale or Windows execution is unavailable")
            job = self.coordinator.reserve(OperationType.RENAME)
        except Exception as error:
            return RenameResult(RenameState.REJECTED, None, not_processed, str(error))

        journal = None
        try:
            run = _RenameRun(
                plan,
                preflight.reviewed_plan.entries,
                self.journals.managed_paths,
                job.job_id,
                progress,
            )
            run.report("preparing")
            if self._cancelled(job.job_id):
                return RenameResult(RenameState.CANCELLED, None, not_processed)
            run.revalidate(lambda: self._cancelled(job.job_id))
            if self._cancelled(job.job_id):
                return RenameResult(RenameState.CANCELLED, None, not_processed)
            self._check_recovery_scope(plan)
            if current_preflight() is not preflight:
                return RenameResult(
                    RenameState.REJECTED, None, not_processed, "Preflight superseded"
                )
            journal = self.journals.prepare(TransactionRecord.from_rename_preflight(preflight))
            identifier = journal.record.transaction_id
            if self._cancelled(job.job_id):
                return RenameResult(RenameState.CANCELLED, identifier, not_processed)
            if current_preflight() is not preflight:
                return RenameResult(
                    RenameState.REJECTED,
                    identifier,
                    not_processed,
                    "Preflight superseded",
                )
            self.coordinator.begin_modification(job.job_id, journal)
            self._check_recovery_scope(plan, ignore_transaction_id=journal.record.transaction_id)
            return run.execute(journal, lambda: current_preflight() is preflight)
        except Exception as error:
            identifier = journal.record.transaction_id if journal is not None else None
            active = self.coordinator.current
            if journal is not None and active is not None and active.modifying:
                detail = _retain(journal, str(error))
                return RenameResult(
                    RenameState.RECOVERY_REQUIRED, identifier, not_processed, detail
                )
            if self._cancelled(job.job_id):
                return RenameResult(RenameState.CANCELLED, identifier, not_processed, str(error))
            return RenameResult(RenameState.FAILED_SAFE, identifier, not_processed, str(error))
        finally:
            if journal is not None and journal.can_modify:
                _retain(journal, "Rename stopped before verification")
            self.coordinator.finish(job.job_id)

    def _check_recovery_scope(self, plan: DestinationPlan, *, ignore_transaction_id=None) -> None:
        paths = tuple(
            path
            for row in plan.entries
            for path in (row.source_path, row.destination_path)
            if path is not None
        )
        observations = tuple(
            observation
            for row in plan.entries
            for observation in (row.source_observation, row.destination_observation)
            if observation is not None
        )
        RecoveryGuard(self.journals).check(
            paths, observations, ignore_transaction_id=ignore_transaction_id
        )

    def _cancelled(self, job_id: UUID) -> bool:
        current = self.coordinator.current
        return bool(current and current.job_id == job_id and current.cancel_requested)


def _retain(journal: JournalSession, detail: str) -> str:
    if journal.can_modify:
        try:
            journal.checkpoint(recovery_required=detail)
        except OSError as error:
            return f"{detail}. Could not record recovery state: {error}"
    return detail


def _entry_key(path: str, observation: PathObservation | None):
    return (
        observation.volume_guid if observation is not None else None,
        observation.parent_identity if observation is not None else None,
        path_key(ntpath.basename(path)),
    )


class _RenameRun:
    def __init__(
        self,
        plan: DestinationPlan,
        reviewed_rows: tuple[DestinationEntry, ...],
        managed_paths: tuple[str, ...],
        job_id: UUID,
        callback: Callable[[RenameProgress], None] | None,
    ) -> None:
        self.options = replace(
            plan.options, managed_paths=plan.options.managed_paths + managed_paths
        )
        self.plan = plan
        self.job_id = job_id
        self.callback = callback
        self.rows = {row.entry_id: row for row in plan.entries}
        self.indices = {row.entry_id: index for index, row in enumerate(plan.entries)}
        self.reserved = {
            path_key(path)
            for row in reviewed_rows
            for path in (row.source_path, row.destination_path)
            if path is not None
        }
        self.reserved_entries = {
            _entry_key(path, observation)
            for row in reviewed_rows
            for path, observation in (
                (row.source_path, row.source_observation),
                (row.destination_path, row.destination_observation),
            )
            if path is not None and observation is not None
        }
        self.installed: set[UUID] = set()
        self.staged: dict[UUID, PathObservation] = {}
        self.metadata: dict[
            tuple[str | None, FileSystemIdentitySnapshot], WindowsObjectObservation
        ] = {}
        self.failed_entry: UUID | None = None
        self.failed_move: tuple[str, str] | None = None

    def report(
        self,
        stage: str,
        row: DestinationEntry | None = None,
        source: str | None = None,
        target: str | None = None,
    ) -> None:
        if self.callback is not None:
            self.callback(
                RenameProgress(
                    self.job_id,
                    stage,
                    len(self.installed),
                    len(self.rows),
                    row.entry_id if row is not None else None,
                    source,
                    target,
                )
            )

    def _observer(self) -> tuple[WindowsPathObserver, PathObserver]:
        native = WindowsPathObserver(self.options.managed_paths, capture_identity=True)
        return native, planning_observer(self.options, native)

    def revalidate(self, cancelled: Callable[[], bool]) -> None:
        _, observe = self._observer()

        def cancellable_probe(path: str) -> PathObservation:
            if cancelled():
                raise RuntimeError("Rename preparation cancelled")
            return observe(path)

        fresh = reobserve_destination_set(
            replace(self.plan, options=self.options),
            tuple(self.rows),
            observe=cancellable_probe,
        )
        if fresh.entries != self.plan.entries:
            old, new = next(
                (before, after)
                for before, after in zip(self.plan.entries, fresh.entries, strict=True)
                if before != after
            )
            raise OSError(
                f"Reviewed source or destination changed: {old.source_path} -> "
                f"{old.destination_path} ({new.reason or 'identity, occupancy or metadata'})"
            )
        if (
            fresh.state is not PlanState.VALID
            or fresh.dependency_groups != self.plan.dependency_groups
        ):
            raise OSError("The reviewed dependency groups no longer match")

    def _current(self, observation: PathObservation) -> PathObservation:
        metadata = observation.object_metadata
        assert metadata is not None
        current = self.metadata.get((observation.volume_guid, metadata.identity), metadata)
        return replace(observation, object_metadata=current)

    def execute(self, journal: JournalSession, is_current: Callable[[], bool]) -> RenameResult:
        detail = None
        try:
            self.report("modifying")
            if not is_current():
                result = self._finish(journal, "Preflight superseded before the first move")
                if result.state is RenameState.FAILED_SAFE:
                    return replace(result, state=RenameState.REJECTED)
                return result
            for group in self.plan.dependency_groups:
                if not group.requires_staging:
                    for identifier in group.entry_ids:
                        self._move(journal, self.rows[identifier])
                    continue
                pivot, *others = group.entry_ids
                self._move(journal, self.rows[pivot], stage_first=True)
                for identifier in reversed(others):
                    self._move(journal, self.rows[identifier])
                self._move(journal, self.rows[pivot], from_stage=True)
            self.report("verifying")
        except Exception as error:
            detail = str(error)
            if self.failed_move is not None:
                source, target = self.failed_move
                detail = f"{source} -> {target} — {detail}"
        return self._finish(journal, detail)

    def _move(
        self,
        journal: JournalSession,
        row: DestinationEntry,
        *,
        stage_first: bool = False,
        from_stage: bool = False,
    ) -> None:
        self.failed_entry = row.entry_id
        stored = journal.record.entries[self.indices[row.entry_id]]
        source, target = row.source_path, _destination(row)
        source_baseline, target_baseline = row.source_observation, row.destination_observation
        assert source_baseline is not None and target_baseline is not None
        stage = "before_rename"
        if from_stage:
            assert stored.staging_path is not None
            source, source_baseline = stored.staging_path, self.staged[row.entry_id]
            stage = "before_install_staged"
        if stage_first:
            target = _new_staging_path(source)
            key = _entry_key(target, source_baseline)
            if (
                path_key(target) in self.reserved
                or key in self.reserved_entries
                or ntpath.dirname(target) != ntpath.dirname(source)
            ):
                raise OSError(f"Staging name conflicts with the reviewed paths: {target}")
            self.reserved.add(path_key(target))
            self.reserved_entries.add(key)
            target_baseline = source_baseline
            stage = "before_staging"
            stored = replace(stored, staging_path=target)
        self.failed_move = source, target
        observer, observe = self._observer()
        original, vacant = observe(source), observe(target)
        if not _expected_file(original, self._current(source_baseline)):
            raise OSError(f"Source cannot be verified: {source} ({original.reason or 'changed'})")
        if (
            not _parents_match(vacant, target_baseline)
            or vacant.occupancy is not PathOccupancy.VACANT
        ):
            raise OSError(
                f"Destination is not verified vacant: {target} ({vacant.reason or 'changed'})"
            )
        if original.read_only_volume or vacant.read_only_volume or not vacant.preserves_case:
            raise OSError(f"The volume cannot preserve the planned rename: {target}")

        with bind_rename(observer, source, original, target, vacant) as bound:
            journal.checkpoint(replace(stored, stage=stage))
            self.report(stage, row, source, target)
            journal.require_modification()
            _, immediate = self._observer()
            if immediate(source) != original or immediate(target) != vacant:
                raise OSError("Source or destination changed after the checkpoint")
            bound.move()
        _, observe_after = self._observer()
        actual, previous = observe_after(target), observe_after(source)
        expected = replace(
            vacant, actual_name=ntpath.basename(target), object_metadata=original.object_metadata
        )
        if (
            not _expected_file(actual, expected, renamed=True)
            or previous.occupancy is not PathOccupancy.VACANT
            or not _parents_match(previous, original)
        ):
            raise OSError(f"The completed move could not be verified: {source} -> {target}")
        metadata = actual.object_metadata
        assert metadata is not None
        self.metadata[(actual.volume_guid, metadata.identity)] = metadata
        if stage_first:
            self.staged[row.entry_id] = actual
            journal.checkpoint(replace(stored, stage="staged"))
        else:
            self.installed.add(row.entry_id)
            self.staged.pop(row.entry_id, None)
            journal.checkpoint(replace(stored, stage="installed", outcome=EntryOutcome.SUCCEEDED))
        self.failed_entry = None
        self.failed_move = None

    def _finish(self, journal: JournalSession, detail: str | None) -> RenameResult:
        _, observe = self._observer()
        outcomes = []
        evidence = []
        record = journal.record
        for stored in record.entries:
            row = self.rows[stored.entry_id]
            target, status, proof = self._verify_entry(row, stored, observe)
            reason = detail if row.entry_id == self.failed_entry else None
            if status is RenameOutcome.RECOVERY_REQUIRED and reason is None:
                reason = "The expected file location or staging vacancy could not be verified"
            outcomes.append(RenameEntryResult(
                row.entry_id, status, target, reason,
                proof.observation if proof is not None else None,
            ))
            if proof is not None:
                evidence.append(proof)

        if journal.can_modify and len(evidence) == len(outcomes):
            try:
                journal.finalize(tuple(evidence))
            except Exception as error:
                detail = f"{detail}. {error}" if detail else str(error)
            else:
                state = (
                    RenameState.FAILED_SAFE
                    if detail or any(item.outcome is VerifiedOutcome.ORIGINAL for item in evidence)
                    else RenameState.SUCCEEDED
                )
                return RenameResult(state, record.transaction_id, tuple(outcomes), detail)
        detail = _retain(
            journal, detail or "One or more rename outcomes require recovery inspection"
        )
        return RenameResult(
            RenameState.RECOVERY_REQUIRED, record.transaction_id, tuple(outcomes), detail
        )

    def _verify_entry(
        self,
        row: DestinationEntry,
        stored: TransactionEntry,
        observe: PathObserver,
    ) -> tuple[str | None, RenameOutcome, EntryVerification | None]:
        source = row.source_observation
        destination = row.destination_observation
        assert source is not None and destination is not None
        is_noop = row.operation is PathOperation.NO_OP
        installed = row.entry_id in self.installed
        path = _destination(row) if installed or is_noop else row.source_path
        expected = replace(
            destination if installed else source,
            actual_name=ntpath.basename(path) if installed else source.actual_name,
            object_metadata=self._current(source).object_metadata,
        )
        actual = observe(path)
        observed_path = path if _expected_file(actual, expected) else None
        artifacts = tuple(
            artifact
            for artifact in (stored.staging_path, stored.temporary_path)
            if artifact is not None
        )
        if observed_path is None or any(
            (found := observe(artifact)).occupancy is not PathOccupancy.VACANT
            or not _parents_match(found, source)
            for artifact in artifacts
        ):
            if observed_path is None and stored.staging_path is not None:
                staged = observe(stored.staging_path)
                if _expected_file(
                    staged,
                    replace(
                        expected,
                        actual_name=ntpath.basename(stored.staging_path),
                        actual_parent=source.actual_parent,
                        parent_identity=source.parent_identity,
                    ),
                ):
                    observed_path = stored.staging_path
            return observed_path, RenameOutcome.RECOVERY_REQUIRED, None
        outcome = VerifiedOutcome.DESTINATION if installed or is_noop else VerifiedOutcome.ORIGINAL
        proof = EntryVerification(row.entry_id, outcome, path, actual, artifacts)
        try:
            validate_verification(stored, proof, OperationType.RENAME)
        except ValueError:
            return observed_path, RenameOutcome.RECOVERY_REQUIRED, None
        if installed:
            status = RenameOutcome.RENAMED
        elif is_noop:
            status = RenameOutcome.UNCHANGED
        elif self.failed_entry == row.entry_id:
            status = RenameOutcome.FAILED_SAFE
        else:
            status = RenameOutcome.NOT_PROCESSED
        return path, status, proof
