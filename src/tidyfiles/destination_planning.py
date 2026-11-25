"""Read-only destination planning for an ordered, fully evaluated collection."""

from __future__ import annotations

import ntpath
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable
from uuid import UUID

from tidyfiles.collection import (
    CollectionEntry,
    CollectionSnapshot,
    ImportSessionSnapshot,
    normalize_windows_path,
)
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsPathObserver,
    is_within,
    literal_windows_path,
    path_key,
)
from tidyfiles.filename_integration import (
    DiagnosticKind,
    EvaluationDiagnostic,
    EvaluationSettings,
    EvaluationState,
    FilenameEvaluationResult,
)
from tidyfiles.filesystem_paths import CLUTTER_FILENAMES, is_protected_system_path

PathObserver = Callable[[str], PathObservation]
type _PathKey = tuple[str, str]


class PlanState(StrEnum):
    VALID = "valid"
    COLLISION_BLOCKED = "collision_blocked"
    FORMAT_BLOCKED = "format_blocked"
    INCOMPLETE_IMPORT = "incomplete_import"
    SAFETY_BLOCKED = "safety_blocked"
    INCOMPATIBLE_INPUT = "incompatible_input"


class EntryState(StrEnum):
    PLANNED = "planned"
    COMPETING = "competing_destinations"
    OCCUPIED = "occupied_destination"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    UNSAFE = "unsafe"
    UNVERIFIABLE = "unverifiable"


class PathOperation(StrEnum):
    NO_OP = "no_op"
    CASE_ONLY = "case_only"
    RENAME = "rename"
    RELOCATE = "relocate"


class DependencyKind(StrEnum):
    INDEPENDENT = "independent"
    CHAIN = "chain"
    SWAP = "swap"
    CYCLE = "cycle"


@dataclass(frozen=True, slots=True)
class DestinationOptions:
    single_folder: bool = False
    managed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "managed_paths", tuple(self.managed_paths))


@dataclass(frozen=True, slots=True)
class DestinationEntry:
    entry_id: UUID
    source_path: str
    candidate_filename: str
    destination_path: str | None
    state: EntryState = EntryState.PLANNED
    operation: PathOperation | None = None
    reason: str | None = None
    depends_on: UUID | None = None
    source_observation: PathObservation | None = None
    destination_observation: PathObservation | None = None


@dataclass(frozen=True, slots=True)
class DependencyGroup:
    kind: DependencyKind
    entry_ids: tuple[UUID, ...]
    requires_staging: bool = False


@dataclass(frozen=True, slots=True)
class DestinationPlan:
    collection_id: UUID
    collection_revision: int
    evaluation_request_id: UUID
    expression: str | None
    evaluation_settings: EvaluationSettings
    options: DestinationOptions
    state: PlanState
    entries: tuple[DestinationEntry, ...] = ()
    dependency_groups: tuple[DependencyGroup, ...] = ()
    reason: str | None = None
    evaluation_diagnostics: tuple[EvaluationDiagnostic, ...] = ()
    incomplete_imports: tuple[ImportSessionSnapshot, ...] = ()
    collision_subset_ids: tuple[UUID, ...] = ()
    excluded_entry_ids: tuple[UUID, ...] = ()


def plan_destinations(
    snapshot: CollectionSnapshot,
    evaluation: FilenameEvaluationResult,
    *,
    current_request_id: UUID,
    options: DestinationOptions = DestinationOptions(),
    observe: PathObserver | None = None,
) -> DestinationPlan:
    """Plan from a current evaluation. The caller supplies the authoritative request ID."""
    plan = DestinationPlan(
        snapshot.collection_id,
        snapshot.revision,
        evaluation.request_id,
        evaluation.expression,
        evaluation.settings,
        options,
        PlanState.INCOMPATIBLE_INPUT,
        evaluation_diagnostics=evaluation.diagnostics,
        incomplete_imports=snapshot.incomplete_imports,
    )
    if (
        evaluation.request_id != current_request_id
        or evaluation.collection_id != snapshot.collection_id
        or evaluation.collection_revision != snapshot.revision
        or evaluation.state not in {EvaluationState.VALID, EvaluationState.INVALID}
    ):
        return replace(plan, reason="stale_or_incompatible_evaluation")
    if snapshot.capabilities.is_importing or any(
        not entry.is_finalized for entry in snapshot.entries
    ):
        return replace(plan, reason="collection_not_finalized")
    if (
        len(snapshot.entries) != len(evaluation.entries)
        or any(entry != row.entry for entry, row in zip(snapshot.entries, evaluation.entries))
    ):
        return replace(plan, reason="evaluation_entries_mismatch")
    if evaluation.state is EvaluationState.INVALID:
        if any(
            diagnostic.kind in {
                DiagnosticKind.PARSE, DiagnosticKind.TOKEN, DiagnosticKind.VALIDATION,
                DiagnosticKind.METADATA,
            }
            for diagnostic in evaluation.diagnostics
        ):
            return replace(plan, state=PlanState.FORMAT_BLOCKED, reason="invalid_filename_batch")
        return replace(plan, reason="incompatible_invalid_evaluation")
    batch = evaluation.batch_result
    if (
        not snapshot.entries
        or not evaluation.expression
        or evaluation.diagnostics
        or batch is None
        or not batch.is_valid
        or len(batch.files) != len(evaluation.entries)
        or any(
            not row.is_valid or row.file_result != file
            for row, file in zip(evaluation.entries, batch.files)
        )
    ):
        return replace(plan, reason="incomplete_valid_evaluation")
    if plan.incomplete_imports:
        return replace(plan, state=PlanState.INCOMPLETE_IMPORT, reason="incomplete_import")

    entries = []
    for row in evaluation.entries:
        candidate = row.validated_filename
        assert candidate is not None
        if not candidate or candidate in {".", ".."} or ntpath.isreserved(candidate):
            return replace(plan, reason="invalid_validated_component")
        if ntpath.basename(candidate) != candidate:
            return replace(plan, reason="invalid_validated_component")
        entry = row.entry
        entries.append(
            DestinationEntry(
                entry.entry_id,
                entry.source_path,
                candidate,
                _destination(entry, candidate, options.single_folder),
            )
        )
    return _classify(plan, tuple(entries), observe)


def replan_collision_subset(
    reviewed: DestinationPlan,
    snapshot: CollectionSnapshot,
    *,
    current_request_id: UUID,
    current_options: DestinationOptions,
    observe: PathObserver | None = None,
) -> DestinationPlan:
    """Observe the collision-only subset again using the reviewed names and options."""
    plan = replace(
        reviewed,
        state=PlanState.INCOMPATIBLE_INPUT,
        entries=(),
        dependency_groups=(),
        collision_subset_ids=(),
        incomplete_imports=snapshot.incomplete_imports,
    )
    if (
        snapshot.collection_id != reviewed.collection_id
        or snapshot.revision != reviewed.collection_revision
        or current_request_id != reviewed.evaluation_request_id
        or current_options != reviewed.options
        or snapshot.capabilities.is_importing
    ):
        return replace(plan, reason="stale_reviewed_plan")
    if snapshot.has_incomplete_imports:
        return replace(plan, state=PlanState.INCOMPLETE_IMPORT, reason="incomplete_import")
    if reviewed.state is not PlanState.COLLISION_BLOCKED or not reviewed.collision_subset_ids:
        return replace(plan, reason="no_collision_only_subset")
    result = reobserve_destination_set(reviewed, reviewed.collision_subset_ids, observe=observe)
    included = set(reviewed.collision_subset_ids)
    return replace(result, excluded_entry_ids=tuple(
        row.entry_id for row in snapshot.entries if row.entry_id not in included
    ))


def reobserve_destination_set(
    plan: DestinationPlan, entry_ids: tuple[UUID, ...], *, observe: PathObserver | None = None
) -> DestinationPlan:
    """Reclassify an exact ordered projection without changing filenames or execution scope."""
    included = set(entry_ids)
    entries = tuple(
        DestinationEntry(
            row.entry_id, row.source_path, row.candidate_filename, row.destination_path
        )
        for row in plan.entries
        if row.entry_id in included
    )
    if not entries or tuple(row.entry_id for row in entries) != entry_ids:
        return replace(plan, state=PlanState.INCOMPATIBLE_INPUT, reason="invalid_destination_set")
    plan = replace(
        plan,
        excluded_entry_ids=tuple(
            row.entry_id for row in plan.entries if row.entry_id not in included
        ),
    )
    return _classify(plan, entries, observe)


def _destination(entry: CollectionEntry, candidate: str, single_folder: bool) -> str | None:
    source_parent, source_name = ntpath.split(entry.source_path)
    if not source_parent or not source_name:
        return None
    if not single_folder:
        return entry.source_path[:-len(source_name)] + candidate
    root = entry.import_root_path
    if root is None or literal_windows_path(root, allow_root=True) is None:
        return None
    source_parent_key = normalize_windows_path(source_parent)
    root_key = normalize_windows_path(root)
    if not is_within(source_parent_key, root_key):
        return None
    if root.replace("/", "\\") == source_parent.replace("/", "\\"):
        return entry.source_path[:-len(source_name)] + candidate
    return ntpath.join(root, candidate)


def _observed_path_key(path: str, observation: PathObservation) -> _PathKey:
    parent, name = ntpath.split(path)
    # Folding a directory spelling can group physically separate Unicode paths.
    if observation.volume_key is not None and observation.actual_parent is not None:
        return observation.volume_key + observation.actual_parent, path_key(name)
    return path_key(parent), path_key(name)


def planning_observer(options: DestinationOptions, observe: PathObserver) -> PathObserver:
    """Apply the planner's path exclusions and cache literal probes within one pass."""
    managed = (
        tuple(path_key(path) for path in options.managed_paths)
        if all(literal_windows_path(path, allow_root=True) for path in options.managed_paths)
        else None
    )
    observations: dict[str, PathObservation] = {}

    def probe(path: str) -> PathObservation:
        if managed is None:
            return PathObservation(PathOccupancy.UNVERIFIABLE, reason="managed_path_unverifiable")
        literal = literal_windows_path(path)
        if literal is None:
            return PathObservation(PathOccupancy.UNVERIFIABLE, reason="unsupported_path")
        if literal not in observations:
            key = path_key(literal)
            if (
                is_protected_system_path(literal)
                or ntpath.basename(literal).casefold() in CLUTTER_FILENAMES
                or any(is_within(key, root) for root in managed)
            ):
                result = PathObservation(PathOccupancy.UNSAFE, reason="protected_path")
            else:
                try:
                    result = observe(literal)
                except OSError as error:
                    code = getattr(error, "winerror", None) or error.errno or type(error).__name__
                    result = PathObservation(PathOccupancy.UNVERIFIABLE, reason=f"os_error_{code}")
            observations[literal] = result
        return observations[literal]

    return probe


def _classify(
    plan: DestinationPlan, entries: tuple[DestinationEntry, ...], observe: PathObserver | None
) -> DestinationPlan:
    if any(
        literal_windows_path(path, allow_root=True) is None for path in plan.options.managed_paths
    ):
        return replace(plan, state=PlanState.SAFETY_BLOCKED, reason="managed_path_unverifiable")
    observer = observe if observe is not None else WindowsPathObserver(plan.options.managed_paths)
    probe = planning_observer(plan.options, observer)
    rows = [_observe_entry(entry, probe, plan.options.single_folder) for entry in entries]
    sources = defaultdict(list)
    targets = Counter()
    for row, source_key, target_key in rows:
        if source_key is not None:
            sources[source_key].append(row)
        if target_key is not None:
            targets[target_key] += 1
    classified = [
        _classify_destination(row, source_key, target_key, sources, targets)
        for row, source_key, target_key in rows
    ]
    classified = _propagate_blockers(classified)
    if any(row.state in {EntryState.UNSAFE, EntryState.UNVERIFIABLE} for row in classified):
        state = PlanState.SAFETY_BLOCKED
        subset = ()
    elif any(row.state is not EntryState.PLANNED for row in classified):
        state = PlanState.COLLISION_BLOCKED
        subset = tuple(row.entry_id for row in classified if row.state is EntryState.PLANNED)
    else:
        state = PlanState.VALID
        subset = ()
    return replace(
        plan,
        state=state,
        entries=tuple(classified),
        dependency_groups=_groups(classified),
        reason=None,
        collision_subset_ids=subset,
    )


def _observe_entry(
    row: DestinationEntry, probe: PathObserver, single_folder: bool
) -> tuple[DestinationEntry, _PathKey | None, _PathKey | None]:
    source = probe(row.source_path)
    source_key = None
    if source.occupancy is PathOccupancy.FILE and source.actual_name is not None:
        source_key = _observed_path_key(row.source_path, source)
    if row.destination_path is None:
        return (
            replace(
                row, state=EntryState.UNVERIFIABLE, reason="destination_parent_unavailable",
                source_observation=source,
            ),
            source_key,
            None,
        )
    target = probe(row.destination_path)
    target_key = None
    if target.occupancy is PathOccupancy.VACANT or (
        target.occupancy is PathOccupancy.FILE and target.actual_name is not None
    ):
        target_key = _observed_path_key(row.destination_path, target)
    row = replace(row, source_observation=source, destination_observation=target)
    if source_key is None or target_key is None:
        if source.occupancy is PathOccupancy.UNSAFE:
            row = replace(row, state=EntryState.UNSAFE, reason=f"source_{source.reason}")
        elif target.occupancy is PathOccupancy.UNSAFE:
            row = replace(row, state=EntryState.UNSAFE, reason=f"destination_{target.reason}")
        elif source_key is None:
            row = replace(
                row, state=EntryState.UNVERIFIABLE, reason=f"source_{source.reason or 'missing'}"
            )
        else:
            reason = target.reason or "observation_incomplete"
            row = replace(row, state=EntryState.UNVERIFIABLE, reason=f"destination_{reason}")
        return row, source_key, target_key
    if single_folder and source.actual_parent is not None and target.actual_parent is not None:
        if source.volume_key != target.volume_key or not is_within(
            source.actual_parent, target.actual_parent
        ):
            row = replace(row, state=EntryState.UNSAFE, reason="destination_outside_import_root")
            return row, source_key, target_key
    if row.source_path == row.destination_path or (
        source_key == target_key
        and target.actual_name == source.actual_name
        and row.candidate_filename == ntpath.basename(row.source_path)
    ):
        operation = PathOperation.NO_OP
    elif source_key == target_key and target.actual_name == source.actual_name:
        operation = PathOperation.CASE_ONLY
    elif source_key[0] == target_key[0]:
        operation = PathOperation.RENAME
    else:
        operation = PathOperation.RELOCATE
    row = replace(row, operation=operation)
    if operation is not PathOperation.NO_OP and (
        source.read_only_volume or target.read_only_volume
    ):
        row = replace(row, state=EntryState.UNSAFE, reason="read_only_volume")
    if operation is PathOperation.CASE_ONLY and not target.preserves_case:
        row = replace(row, state=EntryState.UNSAFE, reason="volume_does_not_preserve_case")
    return row, source_key, target_key


def _classify_destination(
    row: DestinationEntry,
    source_key: _PathKey | None,
    target_key: _PathKey | None,
    sources: dict[_PathKey, list[DestinationEntry]],
    targets: Counter[_PathKey],
) -> DestinationEntry:
    if row.state is not EntryState.PLANNED:
        return row
    assert source_key is not None and target_key is not None
    if len(sources[source_key]) > 1:
        return replace(row, state=EntryState.UNSAFE, reason="ambiguous_source_path")
    if targets[target_key] > 1:
        return replace(row, state=EntryState.COMPETING, reason="shared_destination")
    target = row.destination_observation
    assert target is not None
    if target.occupancy is PathOccupancy.VACANT:
        return row
    occupants = sources.get(target_key, ())
    if len(occupants) == 1:
        occupant = occupants[0]
        assert occupant.source_observation is not None
        if target.actual_name == occupant.source_observation.actual_name:
            if occupant.entry_id == row.entry_id:
                return row
            if occupant.operation in {PathOperation.RENAME, PathOperation.RELOCATE, None}:
                return replace(row, depends_on=occupant.entry_id)
            return replace(
                row, state=EntryState.OCCUPIED, reason="batch_source_does_not_vacate",
                depends_on=occupant.entry_id,
            )
    return replace(row, state=EntryState.OCCUPIED, reason="unrelated_occupant")


def _propagate_blockers(rows: list[DestinationEntry]) -> list[DestinationEntry]:
    dependents = defaultdict(list)
    blocked = deque()
    for index, row in enumerate(rows):
        if row.state is not EntryState.PLANNED:
            blocked.append(row.entry_id)
        elif row.depends_on is not None:
            dependents[row.depends_on].append(index)
    while blocked:
        for index in dependents.get(blocked.popleft(), ()):
            row = rows[index]
            rows[index] = replace(
                row, state=EntryState.DEPENDENCY_BLOCKED, reason="source_will_not_be_vacated"
            )
            blocked.append(row.entry_id)
    return rows


def _groups(rows: list[DestinationEntry]) -> tuple[DependencyGroup, ...]:
    moves = {
        row.entry_id: row
        for row in rows
        if row.state is EntryState.PLANNED and row.operation is not PathOperation.NO_OP
    }
    preceding = {
        row.depends_on: row.entry_id for row in moves.values() if row.depends_on is not None
    }
    visited = set()
    groups = []
    for row in moves.values():
        if row.depends_on is not None:
            continue
        chain = []
        entry_id: UUID | None = row.entry_id
        while entry_id is not None:
            visited.add(entry_id)
            chain.append(entry_id)
            entry_id = preceding.get(entry_id)
        kind = DependencyKind.INDEPENDENT if len(chain) == 1 else DependencyKind.CHAIN
        groups.append(DependencyGroup(kind, tuple(chain), row.operation is PathOperation.CASE_ONLY))
    for start in moves:
        if start in visited:
            continue
        cycle = []
        entry_id = start
        while entry_id not in visited:
            visited.add(entry_id)
            cycle.append(entry_id)
            dependency = moves[entry_id].depends_on
            assert dependency is not None
            entry_id = dependency
        kind = DependencyKind.SWAP if len(cycle) == 2 else DependencyKind.CYCLE
        groups.append(DependencyGroup(kind, tuple(cycle), requires_staging=True))
    return tuple(groups)
