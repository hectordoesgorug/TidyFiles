"""Read-only revalidation of an exact reviewed rename operation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Callable
from uuid import UUID, uuid4

from tidyfiles.collection import CollectionEntry, CollectionSnapshot, ImportSessionSnapshot
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsPathObserver,
    literal_windows_path,
)
from tidyfiles.destination_planning import (
    DestinationOptions,
    DestinationPlan,
    EntryState,
    PathObserver,
    PlanState,
    plan_destinations,
    planning_observer,
    reobserve_destination_set,
)
from tidyfiles.filename_integration import (
    CandidateChange,
    EvaluationProgress,
    EvaluationSettings,
    EvaluationStage,
    EvaluationState,
    FilenameEvaluationResult,
    compare_candidate_filenames,
    evaluate_collection,
)


class PreflightState(StrEnum):
    PASSED = "passed"
    REVIEW_REQUIRED = "review_required"
    FORMAT_BLOCKED = "format_blocked"
    COLLISION_BLOCKED = "collision_blocked"
    SOURCE_CHANGED = "source_changed"
    SAFETY_BLOCKED = "safety_blocked"
    UNVERIFIABLE = "unverifiable"
    INCOMPLETE_IMPORT = "incomplete_import"
    STALE_INPUT = "stale_input"
    CANCELLED = "cancelled"
    INTERNAL_FAILURE = "internal_failure"


class PreflightStage(StrEnum):
    INPUT = "input"
    SOURCES = "sources"
    METADATA = "metadata"
    EVALUATION = "evaluation"
    COMPARISON = "comparison"
    DESTINATIONS = "destinations"
    FINALIZATION = "finalization"


@dataclass(frozen=True, slots=True)
class PreflightProgress:
    request_id: UUID
    stage: PreflightStage
    completed: int = 0
    total: int = 0


@dataclass(frozen=True, slots=True)
class PreflightRequest:
    snapshot: CollectionSnapshot
    reviewed_plan: DestinationPlan
    reviewed_evaluation: FilenameEvaluationResult
    current_evaluation_id: UUID
    expression: str
    settings: EvaluationSettings
    options: DestinationOptions
    intended_entry_ids: tuple[UUID, ...]
    request_id: UUID = field(default_factory=uuid4)
    acknowledged_incomplete_import_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "intended_entry_ids", tuple(self.intended_entry_ids))
        object.__setattr__(
            self,
            "acknowledged_incomplete_import_ids",
            tuple(self.acknowledged_incomplete_import_ids),
        )


@dataclass(frozen=True, slots=True)
class PreflightDiagnostic:
    code: str
    entry_id: UUID | None = None
    path: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DestinationChange:
    entry_id: UUID
    reviewed_path: str | None
    refreshed_path: str | None


@dataclass(frozen=True, slots=True)
class PreflightResult:
    request_id: UUID
    reviewed_plan: DestinationPlan
    state: PreflightState
    intended_entry_ids: tuple[UUID, ...]
    excluded_entry_ids: tuple[UUID, ...]
    incomplete_imports: tuple[ImportSessionSnapshot, ...]
    diagnostic: PreflightDiagnostic
    fresh_evaluation: FilenameEvaluationResult | None = None
    fresh_plan: DestinationPlan | None = None
    source_observations: tuple[tuple[UUID, PathObservation], ...] = ()
    candidate_changes: tuple[CandidateChange, ...] = ()
    destination_changes: tuple[DestinationChange, ...] = ()
    acknowledged_incomplete_import_ids: tuple[UUID, ...] = ()


class _Cancelled(Exception):
    pass


def capture_review_plan(
    snapshot: CollectionSnapshot,
    evaluation: FilenameEvaluationResult,
    *,
    current_request_id: UUID,
    options: DestinationOptions = DestinationOptions(),
    acknowledged_incomplete_import_ids: tuple[UUID, ...] = (),
    observe: PathObserver | None = None,
) -> DestinationPlan:
    """Acquire the stronger baseline before showing a destination plan for review."""
    return plan_destinations(
        snapshot,
        evaluation,
        current_request_id=current_request_id,
        options=options,
        acknowledged_incomplete_import_ids=acknowledged_incomplete_import_ids,
        observe=observe if observe is not None else _native_observer(options),
    )


def _native_observer(options: DestinationOptions) -> PathObserver:
    return WindowsPathObserver(options.managed_paths, capture_identity=True)


def _initial_result(request: PreflightRequest) -> PreflightResult:
    included = set(request.intended_entry_ids)
    return PreflightResult(
        request.request_id,
        request.reviewed_plan,
        PreflightState.STALE_INPUT,
        request.intended_entry_ids,
        tuple(
            entry.entry_id for entry in request.snapshot.entries if entry.entry_id not in included
        ),
        request.snapshot.incomplete_imports,
        PreflightDiagnostic("input_unvalidated"),
        acknowledged_incomplete_import_ids=request.acknowledged_incomplete_import_ids,
    )


def run_preflight(
    request: PreflightRequest,
    *,
    observer_factory: Callable[[DestinationOptions], PathObserver] = _native_observer,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[PreflightProgress], None] | None = None,
) -> PreflightResult:
    """Run off the Tk thread. Each pass owns its observations and releases native handles."""
    result = _initial_result(request)
    snapshot = request.snapshot
    review = request.reviewed_plan
    evaluated = request.reviewed_evaluation
    intended = request.intended_entry_ids

    def stop(
        state: PreflightState,
        code: str,
        entry_id: UUID | None = None,
        path: str | None = None,
        detail: str | None = None,
    ) -> PreflightResult:
        return replace(
            result, state=state, diagnostic=PreflightDiagnostic(code, entry_id, path, detail)
        )

    def check() -> None:
        if cancel_check is not None and cancel_check():
            raise _Cancelled

    def progress(stage: PreflightStage, completed: int = 0, total: int = 0) -> None:
        check()
        if progress_callback is not None:
            progress_callback(PreflightProgress(request.request_id, stage, completed, total))
        check()

    def evaluation_progress(update: EvaluationProgress) -> None:
        if update.completed in (0, update.total) or update.completed % 128 == 0:
            stage = (
                PreflightStage.METADATA
                if update.stage is EvaluationStage.METADATA
                else PreflightStage.EVALUATION
            )
            progress(stage, update.completed, update.total)

    def new_observer() -> PathObserver:
        observe = observer_factory(request.options)

        def checked(path: str) -> PathObservation:
            check()
            observation = observe(path)
            check()
            return observation

        return planning_observer(request.options, checked)

    try:
        progress(PreflightStage.INPUT)
        incomplete_ids = tuple(
            session.session_id for session in snapshot.incomplete_imports
        )
        if (
            incomplete_ids != request.acknowledged_incomplete_import_ids
            or review.acknowledged_incomplete_import_ids != incomplete_ids
        ):
            return stop(
                PreflightState.INCOMPLETE_IMPORT,
                "incomplete_import_acknowledgment_required",
            )
        if not _review_matches(request, check):
            return stop(PreflightState.STALE_INPUT, "review_context_mismatch")
        for entry, row in zip(snapshot.entries, review.entries, strict=True):
            check()
            source, destination = row.source_observation, row.destination_observation
            assert source is not None and destination is not None
            if not _parent_observed(source) or not _parent_observed(destination):
                return stop(
                    PreflightState.UNVERIFIABLE,
                    "review_parent_identity_missing",
                    entry.entry_id,
                    row.destination_path,
                )
            failure = _source_failure(entry, source, source)
            if failure is not None:
                return stop(*failure, entry.entry_id, entry.source_path)
            if destination.occupancy is PathOccupancy.FILE and destination.object_metadata is None:
                return stop(
                    PreflightState.UNVERIFIABLE,
                    "review_occupant_identity_missing",
                    entry.entry_id,
                    row.destination_path,
                )

        total = len(snapshot.entries)
        progress(PreflightStage.SOURCES, 0, total)
        observe = new_observer()
        sources = []
        for index, (entry, row) in enumerate(zip(snapshot.entries, review.entries, strict=True), 1):
            check()
            current = observe(entry.source_path)
            sources.append((entry.entry_id, current))
            failure = _source_failure(entry, row.source_observation, current)
            if failure is not None:
                result = replace(result, source_observations=tuple(sources))
                return stop(*failure, entry.entry_id, entry.source_path, current.reason)
            if index % 128 == 0 or index == total:
                progress(PreflightStage.SOURCES, index, total)
        result = replace(result, source_observations=tuple(sources))

        fresh = evaluate_collection(
            snapshot,
            request.expression,
            settings=request.settings,
            request_id=request.request_id,
            force_metadata_refresh=True,
            cancel_check=cancel_check,
            progress_callback=evaluation_progress,
        )
        result = replace(result, fresh_evaluation=fresh)
        check()
        if fresh.state is EvaluationState.CANCELLED:
            return stop(PreflightState.CANCELLED, "cancelled")
        if (
            fresh.request_id != request.request_id
            or fresh.collection_id != snapshot.collection_id
            or fresh.collection_revision != snapshot.revision
            or fresh.expression != request.expression
            or fresh.settings != request.settings
            or tuple(row.entry for row in fresh.entries) != snapshot.entries
            or fresh.state not in {EvaluationState.VALID, EvaluationState.INVALID}
        ):
            return stop(PreflightState.STALE_INPUT, "fresh_evaluation_mismatch")
        if fresh.state is EvaluationState.INVALID:
            return stop(PreflightState.FORMAT_BLOCKED, "fresh_filename_evaluation_failed")

        progress(PreflightStage.COMPARISON)
        changes = tuple(
            change
            for change in compare_candidate_filenames(evaluated, fresh)
            if change.previous_validated_filename != change.current_validated_filename
        )
        progress(PreflightStage.DESTINATIONS)
        observe = new_observer()
        full = plan_destinations(
            snapshot,
            fresh,
            current_request_id=request.request_id,
            options=request.options,
            acknowledged_incomplete_import_ids=request.acknowledged_incomplete_import_ids,
            observe=observe,
        )
        check()
        result = replace(result, fresh_plan=full, candidate_changes=changes)
        if full.state is PlanState.INCOMPATIBLE_INPUT:
            return stop(PreflightState.STALE_INPUT, "fresh_plan_mismatch")
        if not full.entries:
            return stop(PreflightState.SAFETY_BLOCKED, full.reason or "fresh_plan_unavailable")
        destination_changes = tuple(
            DestinationChange(old.entry_id, old.destination_path, new.destination_path)
            for old, new in zip(review.entries, full.entries, strict=True)
            if old.destination_path != new.destination_path
        )
        result = replace(result, destination_changes=destination_changes)
        for entry, (_, source), row in zip(snapshot.entries, sources, full.entries, strict=True):
            check()
            if row.source_observation is None:
                return stop(PreflightState.UNVERIFIABLE, "fresh_source_missing", entry.entry_id)
            failure = _source_failure(entry, source, row.source_observation)
            if failure is not None:
                return stop(
                    *failure, entry.entry_id, entry.source_path, row.source_observation.reason
                )
        if changes or destination_changes:
            return stop(PreflightState.REVIEW_REQUIRED, "candidate_or_destination_changed")
        for old, row in zip(review.entries, full.entries, strict=True):
            check()
            if row.state in {EntryState.UNSAFE, EntryState.UNVERIFIABLE}:
                state = (
                    PreflightState.SAFETY_BLOCKED
                    if row.state is EntryState.UNSAFE
                    else PreflightState.UNVERIFIABLE
                )
                return stop(
                    state, row.reason or "unsafe_destination", row.entry_id, row.destination_path
                )
            target = row.destination_observation
            if not _parent_observed(target):
                return stop(
                    PreflightState.UNVERIFIABLE,
                    "fresh_parent_identity_missing",
                    row.entry_id,
                    row.destination_path,
                )
            if _parent_key(old.destination_observation) != _parent_key(target):
                return stop(
                    PreflightState.SAFETY_BLOCKED,
                    "destination_parent_or_volume_changed",
                    row.entry_id,
                    row.destination_path,
                )

        selected = (
            reobserve_destination_set(full, intended, observe=observe)
            if result.excluded_entry_ids
            else full
        )
        result = replace(result, fresh_plan=selected)
        if selected.state is not PlanState.VALID:
            row = next(
                (row for row in selected.entries if row.state is not EntryState.PLANNED), None
            )
            if row is None:
                return stop(PreflightState.INTERNAL_FAILURE, "selected_plan_unavailable")
            return stop(
                PreflightState.COLLISION_BLOCKED,
                row.reason or "destination_collision",
                row.entry_id,
                row.destination_path,
            )
        before = {row.entry_id: row for row in review.entries}
        current_sources = {row.entry_id: row.source_observation for row in selected.entries}
        for row in selected.entries:
            check()
            old = before[row.entry_id]
            target = row.destination_observation
            prior = old.destination_observation
            assert target is not None and prior is not None
            if (row.operation, row.depends_on, target.occupancy, target.actual_name) != (
                old.operation,
                old.depends_on,
                prior.occupancy,
                prior.actual_name,
            ):
                return stop(
                    PreflightState.REVIEW_REQUIRED,
                    "destination_or_dependency_changed",
                    row.entry_id,
                    row.destination_path,
                )
            if target.occupancy is PathOccupancy.FILE:
                occupant = current_sources[row.depends_on or row.entry_id]
                if target.object_metadata is None or occupant is None:
                    return stop(
                        PreflightState.UNVERIFIABLE,
                        "occupant_identity_missing",
                        row.entry_id,
                        row.destination_path,
                    )
                if (
                    target.object_metadata != prior.object_metadata
                    or target.object_metadata != occupant.object_metadata
                ):
                    return stop(
                        PreflightState.SOURCE_CHANGED,
                        "expected_occupant_changed",
                        row.entry_id,
                        row.destination_path,
                    )
        progress(PreflightStage.FINALIZATION)
        return stop(PreflightState.PASSED, "validation_passed")
    except _Cancelled:
        return stop(PreflightState.CANCELLED, "cancelled")
    except Exception as error:
        return stop(
            PreflightState.INTERNAL_FAILURE,
            "unexpected_failure",
            detail=f"{type(error).__name__}: {error}",
        )


def _review_matches(request: PreflightRequest, check: Callable[[], None]) -> bool:
    plan, snapshot, evaluated = request.reviewed_plan, request.snapshot, request.reviewed_evaluation
    incomplete_ids = tuple(
        session.session_id for session in snapshot.incomplete_imports
    )
    if (
        snapshot.capabilities.is_importing
        or request.acknowledged_incomplete_import_ids != incomplete_ids
        or plan.acknowledged_incomplete_import_ids != incomplete_ids
        or plan.excluded_entry_ids
        or request.request_id == request.current_evaluation_id
        or (plan.collection_id, plan.collection_revision)
        != (snapshot.collection_id, snapshot.revision)
        or plan.evaluation_request_id != request.current_evaluation_id
        or (evaluated.expression, evaluated.settings) != (request.expression, request.settings)
        or plan.options != request.options
        or plan.state not in {PlanState.VALID, PlanState.COLLISION_BLOCKED}
    ):
        return False
    expected_ids = (
        tuple(row.entry_id for row in plan.entries)
        if plan.state is PlanState.VALID
        else plan.collision_subset_ids
    )
    if not expected_ids or request.intended_entry_ids != expected_ids:
        return False
    observations: dict[str, PathObservation] = {}
    for row in plan.entries:
        check()
        for path, observation in (
            (row.source_path, row.source_observation),
            (row.destination_path, row.destination_observation),
        ):
            literal = literal_windows_path(path) if path is not None else None
            if literal is None or observation is None:
                return False
            if observations.setdefault(literal, observation) != observation:
                return False
    absent = PathObservation(PathOccupancy.UNVERIFIABLE, reason="missing_review_observation")

    def recorded(path: str) -> PathObservation:
        check()
        return observations.get(path, absent)

    return (
        plan_destinations(
            snapshot,
            evaluated,
            current_request_id=request.current_evaluation_id,
            options=request.options,
            acknowledged_incomplete_import_ids=request.acknowledged_incomplete_import_ids,
            observe=recorded,
        )
        == plan
    )


def _parent_observed(value: PathObservation | None) -> bool:
    return bool(
        value
        and value.volume_guid
        and value.volume_key
        and value.parent_identity
        and value.actual_parent
    )


def _parent_key(value: PathObservation | None) -> tuple[object, ...]:
    assert value is not None
    return value.volume_guid, value.volume_key, value.parent_identity, value.actual_parent


def _source_failure(
    entry: CollectionEntry,
    previous: PathObservation | None,
    current: PathObservation,
) -> tuple[PreflightState, str] | None:
    if current.occupancy is PathOccupancy.UNSAFE:
        return PreflightState.SAFETY_BLOCKED, "unsafe_source"
    if current.occupancy is PathOccupancy.VACANT:
        return PreflightState.SOURCE_CHANGED, "source_missing"
    if current.occupancy is PathOccupancy.UNVERIFIABLE or not _parent_observed(current):
        return PreflightState.UNVERIFIABLE, "source_unverifiable"
    if previous is None or previous.object_metadata is None or current.object_metadata is None:
        return PreflightState.UNVERIFIABLE, "source_identity_baseline_missing"
    original, metadata = entry.filesystem_identity, entry.metadata
    if (
        original is None
        or type(original.volume_id) is not int
        or type(original.file_id) is not int
        or original.file_id <= 0
        or metadata is None
        or metadata.size_bytes is None
        or metadata.modified_time_ns is None
    ):
        return PreflightState.UNVERIFIABLE, "discovery_baseline_unavailable"
    observed = current.object_metadata
    if _parent_key(previous) != _parent_key(current):
        return PreflightState.SAFETY_BLOCKED, "source_parent_or_volume_changed"
    if original != observed.identity or previous.object_metadata.identity != observed.identity:
        return PreflightState.SOURCE_CHANGED, "source_object_changed"
    if (
        previous.actual_name != current.actual_name
        or previous.object_metadata != observed
        or metadata.size_bytes != observed.size_bytes
        or metadata.modified_time_ns != observed.modified_time_ns
        or (
            metadata.created_time_ns is not None
            and metadata.created_time_ns != observed.created_time_ns
        )
        or (metadata.attributes is not None and metadata.attributes != observed.attributes)
    ):
        return PreflightState.SOURCE_CHANGED, "source_metadata_or_name_changed"
    return None
