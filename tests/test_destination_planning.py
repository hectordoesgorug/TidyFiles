from __future__ import annotations

import ntpath
import os
from collections import Counter
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from tidyfiles.collection import ImportDiagnostic, ImportMode, WorkingCollection
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsPathObserver,
    path_key,
)
from tidyfiles.destination_planning import (
    DependencyKind,
    DestinationOptions,
    EntryState,
    PathOperation,
    PlanState,
    plan_destinations,
    replan_collision_subset,
)
from tidyfiles.filename_integration import (
    DiagnosticKind,
    EvaluationMode,
    EvaluationSettings,
    EvaluationState,
    evaluate_collection,
)

_ROOT = r"D:\Photos"
_VACANT = PathObservation(PathOccupancy.VACANT)


def _import(collection, paths, *, root=None, diagnostic=None):
    mode = ImportMode.ADD_FILES if root is None else ImportMode.ADD_FOLDER
    session = collection.begin_import(mode, folder_root=root)
    entries = tuple(collection.register_path(session, path).entry for path in paths)
    collection.finalize_import(
        session, incomplete=diagnostic is not None, diagnostics=(diagnostic,) if diagnostic else ()
    )
    return entries


def _collection(*paths):
    collection = WorkingCollection()
    entries = _import(collection, paths)
    collection.set_order(tuple(entry.entry_id for entry in entries))
    return collection


def _files(*paths):
    files = {
        path_key(str(path)): PathObservation(PathOccupancy.FILE, ntpath.basename(str(path)))
        for path in paths
    }
    return lambda path: files.get(path_key(path), _VACANT)


def _plan(collection, expression, *, observe, options=DestinationOptions()):
    snapshot = collection.snapshot()
    evaluation = evaluate_collection(snapshot, expression)
    return plan_destinations(
        snapshot, evaluation, current_request_id=evaluation.request_id,
        options=options, observe=observe,
    )


def _subset(plan, snapshot, *, observe, request_id=None, options=None):
    return replan_collision_subset(
        plan, snapshot, current_request_id=request_id or plan.evaluation_request_id,
        current_options=options or plan.options, observe=observe,
    )


def _unexpected_probe(path):
    pytest.fail(f"Unexpected filesystem observation for {path}")


@pytest.mark.parametrize(
    ("expression", "operation", "occupancy", "staging"),
    [
        ("Better", PathOperation.RENAME, PathOccupancy.VACANT, False),
        ("<Name>", PathOperation.NO_OP, PathOccupancy.FILE, False),
        ("a", PathOperation.CASE_ONLY, PathOccupancy.FILE, True),
    ],
    ids=["A01-vacant", "A02-no-op", "A03-case-only"],
)
def test_single_rename(expression, operation, occupancy, staging):
    source = ntpath.join(_ROOT, "A.txt")
    plan = _plan(_collection(source), expression, observe=_files(source))
    assert plan.state is PlanState.VALID
    row, = plan.entries
    assert row.operation is operation
    assert row.state is EntryState.PLANNED
    assert row.destination_path == ntpath.join(_ROOT, row.candidate_filename)
    assert row.destination_observation.occupancy is occupancy
    assert row.depends_on is None
    assert any(group.requires_staging for group in plan.dependency_groups) is staging
    assert bool(plan.dependency_groups) is (operation is not PathOperation.NO_OP)


@pytest.mark.parametrize(
    "names", [("Red C.txt", "Blue C.txt"), ("Red C.txt", "Blue c.txt")],
    ids=["A04-same-target", "A24-different-case"],
)
def test_all_competing_targets_are_blocked(names):
    sources = tuple(ntpath.join(_ROOT, name) for name in names)
    plan = _plan(_collection(*sources), "<Word:2>", observe=_files(*sources))
    assert plan.state is PlanState.COLLISION_BLOCKED
    assert [row.state for row in plan.entries] == [EntryState.COMPETING] * 2
    assert not plan.collision_subset_ids


def test_a05_unrelated_file_occupies_destination():
    source, occupant = (ntpath.join(_ROOT, name) for name in ("A.txt", "C.txt"))
    plan = _plan(_collection(source), "C", observe=_files(source, occupant))
    assert plan.state is PlanState.COLLISION_BLOCKED
    row, = plan.entries
    assert row.state is EntryState.OCCUPIED
    assert row.reason == "unrelated_occupant"
    assert row.destination_observation.actual_name == "C.txt"
    assert row.depends_on is None


@pytest.mark.parametrize(
    ("names", "expression", "kind", "group_order"),
    [
        (("AB", "BA"), "<Move1:1>", DependencyKind.SWAP, [0, 1]),
        (("01", "02", "03"), "<IncNr:2>", DependencyKind.CHAIN, [2, 1, 0]),
        (("ABC", "BCA", "CAB"), "<Move1:2>", DependencyKind.CYCLE, [0, 1, 2]),
    ],
    ids=["A06-swap", "A07-chain", "A08-cycle"],
)
def test_rename_dependency_groups(names, expression, kind, group_order):
    sources = tuple(ntpath.join(_ROOT, f"{name}.txt") for name in names)
    snapshot = _collection(*sources)
    plan = _plan(snapshot, expression, observe=_files(*sources))
    assert plan.state is PlanState.VALID
    ids = [row.entry_id for row in plan.entries]
    assert ids == [row.entry_id for row in snapshot.snapshot().entries]
    assert [row.depends_on for row in plan.entries] == [
        *ids[1:], None if kind is DependencyKind.CHAIN else ids[0],
    ]
    group, = plan.dependency_groups
    assert group.kind is kind
    assert group.entry_ids == tuple(ids[index] for index in group_order)
    assert group.requires_staging is (kind is not DependencyKind.CHAIN)


def test_a09_a22_collision_subset_propagates_and_keeps_original_numbering():
    sources = tuple(ntpath.join(_ROOT, f"{name}.txt") for name in ("00", "01", "02", "QQ"))
    unrelated = ntpath.join(_ROOT, "03.txt")
    collection = _collection(*sources)
    observed = _files(*sources, unrelated)
    plan = _plan(collection, "<IncNr>", observe=observed)
    assert plan.state is PlanState.COLLISION_BLOCKED
    first, middle, blocked, independent = plan.entries
    assert [row.state for row in plan.entries] == [
        EntryState.DEPENDENCY_BLOCKED, EntryState.DEPENDENCY_BLOCKED,
        EntryState.OCCUPIED, EntryState.PLANNED,
    ]
    assert first.depends_on == middle.entry_id
    assert middle.depends_on == blocked.entry_id
    assert plan.collision_subset_ids == (independent.entry_id,)
    seen = []

    def observe(path):
        seen.append(path)
        return observed(path)

    subset = _subset(plan, collection.snapshot(), observe=observe)
    assert subset.state is PlanState.VALID
    assert subset.excluded_entry_ids == (first.entry_id, middle.entry_id, blocked.entry_id)
    assert subset.entries == (independent,)
    assert independent.candidate_filename == "04.txt"
    assert seen == [independent.source_path, independent.destination_path]
    assert subset.evaluation_request_id == plan.evaluation_request_id
    assert subset.options == plan.options

    changed = _files(*sources, unrelated, independent.destination_path)
    changed_subset = _subset(plan, collection.snapshot(), observe=changed)
    assert changed_subset.state is PlanState.COLLISION_BLOCKED

    invalid = _subset(
        plan, collection.snapshot(), observe=_unexpected_probe, request_id=uuid4()
    )
    assert invalid.state is PlanState.INCOMPATIBLE_INPUT
    invalid = _subset(
        plan, collection.snapshot(), observe=_unexpected_probe,
        options=DestinationOptions(single_folder=True),
    )
    assert invalid.state is PlanState.INCOMPATIBLE_INPUT


def test_a10_independent_rename_groups_are_isolated():
    sources = tuple(
        ntpath.join(_ROOT, f"{name}.txt")
        for name in ("ABC", "BCA", "CAB", "DEF", "EFD", "FDE", "QQZ")
    )
    plan = _plan(_collection(*sources), "<Move1:2>", observe=_files(*sources))
    assert plan.state is PlanState.VALID
    groups = {group.entry_ids: group.kind for group in plan.dependency_groups}
    ids = [row.entry_id for row in plan.entries]
    assert groups == {
        tuple(ids[:3]): DependencyKind.CYCLE,
        tuple(ids[3:6]): DependencyKind.CYCLE,
        (ids[-1],): DependencyKind.INDEPENDENT,
    }


def test_a11_unchanged_source_is_not_a_vacating_dependency():
    sources = tuple(ntpath.join(_ROOT, name) for name in ("A.txt", "AX.txt"))
    plan = _plan(_collection(*sources), "A", observe=_files(*sources))
    assert plan.state is PlanState.COLLISION_BLOCKED
    unchanged, competing = plan.entries
    assert unchanged.operation is PathOperation.NO_OP
    assert unchanged.state is competing.state is EntryState.COMPETING
    assert competing.depends_on is None
    assert not plan.collision_subset_ids


def test_a12_a13_a14_single_folder_preserves_independent_first_import_roots():
    sources = (
        r"D:\Photos\Day1\A.jpg", r"D:\Photos\Day2\B.jpg", r"E:\Archive\Old\C.jpg",
        r"D:\Photos\Day1\Individual.jpg",
    )
    collection = WorkingCollection()
    individual, = _import(collection, sources[-1:])
    photos = _import(collection, (*sources[:2], sources[-1]), root=_ROOT)
    archive, = _import(collection, sources[2:3], root=r"E:\Archive")
    assert photos[-1].entry_id == individual.entry_id
    collection.set_order((photos[0].entry_id, photos[1].entry_id, archive.entry_id,
                          individual.entry_id))
    plan = _plan(
        collection, "<DirName> <IncNr>", observe=_files(*sources),
        options=DestinationOptions(single_folder=True),
    )
    assert plan.state is PlanState.VALID
    assert [row.destination_path for row in plan.entries] == [
        r"D:\Photos\Photos 01.jpg", r"D:\Photos\Photos 02.jpg",
        r"E:\Archive\Archive 03.jpg", r"D:\Photos\Day1\Day1 04.jpg",
    ]
    assert [row.operation for row in plan.entries] == [
        PathOperation.RELOCATE, PathOperation.RELOCATE, PathOperation.RELOCATE,
        PathOperation.RENAME,
    ]


def test_single_folder_rejects_unsafe_established_destination_without_fallback():
    source = r"D:\Photos\Day1\A.jpg"
    collection = WorkingCollection()
    _import(collection, (source,), root=r"E:\Unrelated")
    plan = _plan(
        collection, "Better", observe=_files(source),
        options=DestinationOptions(single_folder=True),
    )
    assert plan.state is PlanState.SAFETY_BLOCKED
    assert plan.entries[0].destination_path is None
    assert plan.entries[0].reason == "destination_parent_unavailable"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows filesystem metadata")
def test_a15_distinct_hard_links_and_unrelated_hard_link_occupancy(tmp_path):
    a, b, outside = (tmp_path / name for name in ("A.txt", "B.txt", "C.txt"))
    a.write_text("same object", encoding="utf-8")
    os.link(a, b)
    os.link(a, outside)
    assert os.path.samefile(a, b) and os.path.samefile(a, outside)
    plan = _plan(_collection(a, b), "<Name> new", observe=None)
    assert plan.state is PlanState.VALID
    assert len({row.entry_id for row in plan.entries}) == 2
    blocked = _plan(_collection(a), "C", observe=None)
    assert blocked.state is PlanState.COLLISION_BLOCKED
    assert blocked.entries[0].reason == "unrelated_occupant"
    assert blocked.entries[0].depends_on is None


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows filesystem metadata")
def test_a16_real_destination_directory_is_unsafe(tmp_path):
    source = tmp_path / "A.txt"
    source.touch()
    (tmp_path / "Target.txt").mkdir()
    plan = _plan(_collection(source), "Target", observe=None)
    assert plan.state is PlanState.SAFETY_BLOCKED
    assert plan.entries[0].destination_observation.occupancy is PathOccupancy.UNSAFE


@pytest.mark.skipif(os.name != "nt", reason="requires a native Windows junction")
def test_a16_real_junction_is_unsafe_and_its_children_are_not_probed(tmp_path, monkeypatch):
    import _winapi

    source = tmp_path / "A.txt"
    source.touch()
    target = tmp_path / "actual"
    target.mkdir()
    (target / "Child.txt").touch()
    junction = tmp_path / "Redirect.txt"
    _winapi.CreateJunction(str(target), str(junction))
    observer = WindowsPathObserver()
    probed = []
    find = observer._find

    def track(path):
        probed.append(path)
        return find(path)

    monkeypatch.setattr(observer, "_find", track)
    plan = _plan(_collection(source), "Redirect", observe=observer)
    assert plan.state is PlanState.SAFETY_BLOCKED
    assert plan.entries[0].destination_observation.occupancy is PathOccupancy.UNSAFE
    child = str(junction / "Child.txt")
    plan = _plan(_collection(child), "Better", observe=observer)
    assert plan.state is PlanState.SAFETY_BLOCKED
    assert child not in probed
    assert str(junction / "Better.txt") not in probed


def test_a17_inaccessible_parent_blocks_even_if_other_rows_have_collisions():
    sources = (r"D:\Photos\Day1\Alpha.txt", r"E:\Archive\Old\Beta.txt")
    collection = WorkingCollection()
    _import(collection, sources[:1], root=_ROOT)
    _import(collection, sources[1:], root=r"E:\Archive")
    existing = _files(*sources, r"D:\Photos\Alpha new.txt")

    def observe(path):
        if path == r"E:\Archive\Beta new.txt":
            raise PermissionError(13, "access denied")
        return existing(path)

    plan = _plan(
        collection, "<Name> new", observe=observe, options=DestinationOptions(single_folder=True)
    )
    assert plan.state is PlanState.SAFETY_BLOCKED
    assert {row.state for row in plan.entries} == {EntryState.OCCUPIED, EntryState.UNVERIFIABLE}
    assert not plan.collision_subset_ids
    subset = _subset(plan, collection.snapshot(), observe=_unexpected_probe)
    assert subset.state is PlanState.INCOMPATIBLE_INPUT


def test_unsafe_chain_terminal_blocks_dependencies_without_partial_plan():
    sources = tuple(ntpath.join(_ROOT, f"{name}.txt") for name in ("01", "02", "04"))
    existing = _files(*sources)

    def observe(path):
        if path == ntpath.join(_ROOT, "03.txt"):
            return PathObservation(PathOccupancy.UNSAFE, reason="simulated_reparse")
        return existing(path)

    plan = _plan(_collection(*sources), "<IncNr:2>", observe=observe)
    assert plan.state is PlanState.SAFETY_BLOCKED
    first, blocked, competing = plan.entries
    assert first.depends_on == blocked.entry_id
    assert first.state is EntryState.DEPENDENCY_BLOCKED
    assert blocked.state is EntryState.UNSAFE
    assert competing.operation is PathOperation.NO_OP
    assert not plan.collision_subset_ids


@pytest.mark.parametrize("occupancy", [PathOccupancy.VACANT, PathOccupancy.UNSAFE])
def test_a18_missing_or_unsafe_source_blocks_planning(occupancy):
    source = ntpath.join(_ROOT, "A.txt")

    def observe(path):
        return PathObservation(occupancy, reason="simulated_link") if path == source else _VACANT

    plan = _plan(_collection(source), "Better", observe=observe)
    assert plan.state is PlanState.SAFETY_BLOCKED
    assert plan.entries[0].source_observation.occupancy is occupancy
    assert not plan.collision_subset_ids


@pytest.mark.parametrize(
    ("expression", "kind"),
    [("<Name", DiagnosticKind.PARSE), ("<Word:2>", DiagnosticKind.TOKEN)],
    ids=["A19-syntax-error", "A20-file-error"],
)
def test_format_failure_blocks_full_batch_and_subset(expression, kind):
    sources = (ntpath.join(_ROOT, "A Blue.txt"), ntpath.join(_ROOT, "B.txt"))
    collection = _collection(*sources)
    plan = _plan(collection, expression, observe=_unexpected_probe)
    assert plan.state is PlanState.FORMAT_BLOCKED
    diagnostic, = plan.evaluation_diagnostics
    assert diagnostic.kind is kind
    assert diagnostic.source_path == (sources[1] if kind is DiagnosticKind.TOKEN else None)
    assert not plan.entries
    assert not plan.collision_subset_ids
    assert _subset(plan, collection.snapshot(), observe=_unexpected_probe).state is (
        PlanState.INCOMPATIBLE_INPUT
    )


def test_a21_collection_and_request_currency_and_result_shape_are_required():
    first, second = (ntpath.join(_ROOT, name) for name in ("A.txt", "B.txt"))
    collection = _collection(first, second)
    snapshot = collection.snapshot()
    evaluation = evaluate_collection(snapshot, "<Name> old")
    newest = evaluate_collection(snapshot, "<Name> new")
    collection.set_order(tuple(reversed([row.entry_id for row in snapshot.entries])))
    for current_snapshot, result, latest_id in (
        (snapshot, evaluation, newest.request_id),
        (collection.snapshot(), evaluation, evaluation.request_id),
        (_collection(first, second).snapshot(), evaluation, evaluation.request_id),
        (snapshot, replace(evaluation, state=EvaluationState.STALE), evaluation.request_id),
        (snapshot, replace(evaluation, entries=evaluation.entries[:1]), evaluation.request_id),
        (snapshot, replace(evaluation, entries=evaluation.entries[::-1]), evaluation.request_id),
        (snapshot, replace(evaluation, batch_result=None), evaluation.request_id),
    ):
        plan = plan_destinations(
            current_snapshot, result, current_request_id=latest_id, observe=_unexpected_probe
        )
        assert plan.state is PlanState.INCOMPATIBLE_INPUT
    collection.begin_import(ImportMode.ADD_FILES)
    active = _plan(collection, "<Name>", observe=_unexpected_probe)
    assert active.state is PlanState.INCOMPATIBLE_INPUT
    assert active.reason == "collection_not_finalized"


def test_a23_incomplete_import_is_preserved_and_blocks_even_after_later_success():
    first, second = (ntpath.join(_ROOT, name) for name in ("A.txt", "B.txt"))
    collection = WorkingCollection()
    diagnostic = ImportDiagnostic(_ROOT, "permission_denied")
    _import(collection, (first,), root=_ROOT, diagnostic=diagnostic)
    _import(collection, (second,))
    plan = _plan(collection, "<Name>", observe=_unexpected_probe)
    assert plan.state is PlanState.INCOMPLETE_IMPORT
    warning, = plan.incomplete_imports
    assert warning.diagnostics == (diagnostic,)
    assert not plan.collision_subset_ids
    assert _subset(plan, collection.snapshot(), observe=_unexpected_probe).state is (
        PlanState.INCOMPLETE_IMPORT
    )


def test_a25_large_chain_is_deterministic_and_observations_are_indexed():
    count = 10_000
    paths = tuple(ntpath.join(_ROOT, f"{index:05d}.txt") for index in range(count))
    collection = _collection(*paths)
    snapshot = collection.snapshot()
    evaluation = evaluate_collection(snapshot, "<IncNr:00001>")
    seen = Counter()
    files = _files(*paths)

    def observe(path):
        seen[path] += 1
        return files(path)

    def plan_with(observer):
        return plan_destinations(
            snapshot, evaluation, current_request_id=evaluation.request_id, observe=observer
        )

    plan = plan_with(observe)
    assert plan == plan_with(files)
    assert plan.state is PlanState.VALID
    assert len(seen) == count + 1 and max(seen.values()) == 1
    assert plan.dependency_groups[0].entry_ids == tuple(
        row.entry_id for row in reversed(snapshot.entries)
    )


@pytest.mark.parametrize(
    ("root", "options"),
    [
        (r"D:\Managed", DestinationOptions(single_folder=True, managed_paths=(r"d:\MANAGED",))),
        (r"D:\$RECYCLE.BIN", DestinationOptions(single_folder=True)),
        (r"D:\System Volume Information", DestinationOptions(single_folder=True)),
    ],
    ids=["managed", "recycle-bin", "system-volume-information"],
)
def test_a26_protected_paths_are_rejected_before_observation(root, options):
    collection = WorkingCollection()
    _import(collection, (ntpath.join(root, "Nested", "A.txt"),), root=root)
    plan = _plan(collection, "Better", observe=_unexpected_probe, options=options)
    assert plan.state is PlanState.SAFETY_BLOCKED
    assert plan.entries[0].destination_observation.reason == "protected_path"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows filesystem metadata")
def test_a27_native_planning_leaves_tree_contents_and_identities_unchanged(tmp_path, monkeypatch):
    root = tmp_path / "root"
    parent = root / "nested"
    parent.mkdir(parents=True)
    sources = [parent / "A.txt", parent / "B.txt"]
    for index, path in enumerate(sources):
        path.write_text(f"content-{index}", encoding="utf-8")
    (root / "03.txt").write_text("unrelated", encoding="utf-8")
    collection = WorkingCollection()
    _import(collection, sources, root=root)

    def tree():
        return {
            path.relative_to(tmp_path): (
                path.stat().st_ino, None if path.is_dir() else path.read_bytes()
            )
            for path in tmp_path.rglob("*")
        }

    def reject_mutation(*_args, **_kwargs):
        pytest.fail("Planning attempted a filesystem mutation")

    before = tree()
    with monkeypatch.context() as patched:
        for name in ("mkdir", "rename", "replace", "unlink", "remove", "rmdir"):
            patched.setattr(os, name, reject_mutation)
        plan = _plan(collection, "<IncNr:2>", observe=None,
                     options=DestinationOptions(single_folder=True))
        assert plan.state is PlanState.COLLISION_BLOCKED
        subset = _subset(plan, collection.snapshot(), observe=None)
        assert subset.state is PlanState.VALID
        assert Path(subset.entries[0].destination_path) == root / "02.txt"
    assert tree() == before


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows filesystem metadata")
def test_native_case_only_rename_and_possible_short_alias(tmp_path):
    source = tmp_path / "A.txt"
    source.touch()
    collection = _collection(source)
    case_only = _plan(collection, "a", observe=None)
    assert case_only.state is PlanState.VALID
    assert case_only.entries[0].operation is PathOperation.CASE_ONLY
    assert case_only.entries[0].destination_observation.actual_name == "A.txt"
    assert case_only.dependency_groups[0].requires_staging
    potential_alias = _plan(collection, "Long~9", observe=None)
    assert potential_alias.state is PlanState.SAFETY_BLOCKED
    assert potential_alias.entries[0].reason == "destination_possible_short_name_alias"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows directory names")
def test_distinct_directories_with_same_unicode_fold_do_not_share_dependencies(tmp_path):
    left, right = tmp_path / "Straße", tmp_path / "STRASSE"
    left.mkdir()
    right.mkdir()
    first, second, unrelated = left / "01.txt", right / "02.txt", left / "02.txt"
    for path in (first, second, unrelated):
        path.touch()
    assert path_key(str(left)) == path_key(str(right))
    plan = _plan(_collection(first, second), "<IncNr:2>", observe=None)
    assert plan.state is PlanState.COLLISION_BLOCKED
    occupied, independent = plan.entries
    assert occupied.state is EntryState.OCCUPIED and occupied.depends_on is None
    assert independent.state is EntryState.PLANNED
    assert plan.collision_subset_ids == (independent.entry_id,)


@pytest.mark.skipif(os.name != "nt", reason="requires the Windows metadata adapter")
def test_destination_component_limit_from_volume(tmp_path, monkeypatch):
    source = tmp_path / "A.txt"
    source.touch()
    observer = WindowsPathObserver()
    root_drive = ntpath.splitdrive(str(tmp_path))[0]
    volume = observer._inspect_volume(root_drive)
    monkeypatch.setattr(observer, "_volume", lambda _drive: replace(volume, component_limit=8))
    plan = _plan(_collection(source), "Lengthy", observe=observer)
    assert plan.state is PlanState.SAFETY_BLOCKED
    assert plan.entries[0].reason == "destination_volume_component_limit"


def test_filename_evaluation_mode_and_validated_spelling_are_preserved():
    source = ntpath.join(_ROOT, "A.png")
    snapshot = _collection(source).snapshot()
    settings = EvaluationSettings(EvaluationMode.RENAME_AND_REPACK, ".jpg")
    evaluation = evaluate_collection(snapshot, "<Name>?", settings=settings)
    plan = plan_destinations(
        snapshot, evaluation, current_request_id=evaluation.request_id, observe=_files(source)
    )
    row, = plan.entries
    assert plan.state is PlanState.VALID
    assert plan.evaluation_settings == settings
    assert row.candidate_filename == evaluation.entries[0].validated_filename == "A？.jpg"
    assert row.destination_path == ntpath.join(_ROOT, "A？.jpg")
