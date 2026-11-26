from __future__ import annotations

import ntpath
import os
from dataclasses import replace
from threading import Event, get_ident
from uuid import uuid4

import pytest

import tidyfiles.execution_preflight as preflight
from tidyfiles.collection import (
    FileMetadataSnapshot,
    FileSystemIdentitySnapshot,
    ImportDiagnostic,
    ImportMode,
    WorkingCollection,
)
from tidyfiles.destination_observation import (
    PathObservation,
    PathOccupancy,
    WindowsObjectObservation,
    WindowsPathObserver,
)
from tidyfiles.destination_planning import (
    DestinationOptions,
    PathOperation,
    PlanState,
    plan_destinations,
)
from tidyfiles.discovery import DiscoveryState, FilesystemDiscovery
from tidyfiles.execution_preflight import (
    PreflightRequest,
    PreflightStage,
    PreflightState,
    capture_review_plan,
    run_preflight,
)
from tidyfiles.filename_integration import (
    DiagnosticKind,
    DirectoryCountCache,
    EvaluationDiagnostic,
    EvaluationSettings,
    EvaluationState,
    evaluate_collection,
)
from tidyfiles.preflight_service import PreflightService

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows preflight fixtures")


def _collection(paths, *, root=None, metadata=True, incomplete=False):
    collection = WorkingCollection()
    session = collection.begin_import(
        ImportMode.ADD_FOLDER if root else ImportMode.ADD_FILES, folder_root=root
    )
    entries = []
    for path in paths:
        info = path.lstat()
        entry = collection.register_path(
            session,
            path,
            metadata=FileMetadataSnapshot(
                size_bytes=info.st_size,
                modified_time_ns=info.st_mtime_ns,
                created_time_ns=info.st_birthtime_ns,
                attributes=info.st_file_attributes,
            )
            if metadata
            else None,
            filesystem_identity=FileSystemIdentitySnapshot(info.st_dev, info.st_ino)
            if metadata
            else None,
        ).entry
        entries.append(entry.entry_id)
    diagnostics = (ImportDiagnostic(str(paths[0].parent), "access_denied"),) if incomplete else ()
    collection.finalize_import(session, incomplete=incomplete, diagnostics=diagnostics)
    collection.set_order(tuple(entries))
    return collection


def _files(root, *names):
    paths = tuple(root / name for name in names)
    for path in paths:
        path.write_bytes(b"test")
    return paths


def _request(
    collection, expression="<Name> better", *, options=DestinationOptions(), weak=False, cache=None
):
    snapshot = collection.snapshot()
    evaluation = evaluate_collection(snapshot, expression, metadata_cache=cache)
    planner = plan_destinations if weak else capture_review_plan
    plan = planner(snapshot, evaluation, current_request_id=evaluation.request_id, options=options)
    return PreflightRequest(
        snapshot,
        plan,
        evaluation,
        evaluation.request_id,
        expression,
        evaluation.settings,
        options,
        plan.collision_subset_ids or tuple(row.entry_id for row in plan.entries),
    )


def _forbid_observation(_options):
    pytest.fail("Preflight accessed the filesystem before validating the review")


def test_b01_b09_b10_b13_b43_b44_discovery_identity_and_read_only_preflight(tmp_path, monkeypatch):
    paths = _files(tmp_path, "One.txt", "Two.txt")
    collection = WorkingCollection()
    session = FilesystemDiscovery(collection).begin_files(paths)
    session.start()
    report = session.wait_until_terminal()
    assert report.state is DiscoveryState.COMPLETE
    request = _request(collection)
    assert request.reviewed_plan.state is PlanState.VALID

    def tree():
        return {path.name: (path.stat().st_ino, path.read_bytes()) for path in tmp_path.iterdir()}

    def forbidden(*_args, **_kwargs):
        pytest.fail("Unexpected filesystem mutation or directory count")

    before = tree()
    with monkeypatch.context() as patched:
        for name in ("rename", "replace", "unlink", "mkdir", "rmdir", "remove", "scandir"):
            patched.setattr(os, name, forbidden)
        result = run_preflight(request)
    assert result.state is PreflightState.PASSED, result.diagnostic
    assert result.fresh_evaluation.request_id == request.request_id
    assert result.fresh_evaluation.request_id != request.current_evaluation_id
    assert result.fresh_evaluation.directory_enumerations == 0
    assert result.fresh_plan.entries == request.reviewed_plan.entries
    assert result.fresh_plan.entries[0].source_observation.object_metadata.identity == (
        request.snapshot.entries[0].filesystem_identity
    )
    assert collection.snapshot() == request.snapshot
    assert tree() == before


@pytest.mark.parametrize(
    "change",
    [
        "revision",
        "order",
        "membership",
        "expression",
        "settings",
        "options",
        "evaluation",
        "intended_ids",
        "candidate",
        "destination",
    ],
)
def test_b02_to_b06_stale_and_inconsistent_reviews(tmp_path, change):
    request = _request(_collection(_files(tmp_path, "One.txt", "Two.txt")))
    snapshot, review = request.snapshot, request.reviewed_plan
    edits = {
        "revision": {"snapshot": replace(snapshot, revision=snapshot.revision + 1)},
        "order": {"snapshot": replace(snapshot, entries=snapshot.entries[::-1])},
        "membership": {"snapshot": replace(snapshot, entries=snapshot.entries[:1])},
        "expression": {"expression": "<Name> other"},
        "settings": {"settings": EvaluationSettings(max_component_length=100)},
        "options": {"options": DestinationOptions(single_folder=True)},
        "evaluation": {"current_evaluation_id": uuid4()},
        "intended_ids": {"intended_entry_ids": request.intended_entry_ids[::-1]},
        "candidate": {
            "reviewed_plan": replace(
                review,
                entries=(
                    replace(review.entries[0], candidate_filename="Bad.txt"),
                    review.entries[1],
                ),
            )
        },
        "destination": {
            "reviewed_plan": replace(
                review,
                entries=(
                    replace(review.entries[0], destination_path=str(tmp_path / "Bad.txt")),
                    review.entries[1],
                ),
            )
        },
    }
    result = run_preflight(replace(request, **edits[change]), observer_factory=_forbid_observation)
    assert result.state is PreflightState.STALE_INPUT


def test_b07_b08_b12_force_refresh_without_importing_new_files(tmp_path, monkeypatch):
    collection = _collection(_files(tmp_path, "One.txt"))
    cache = DirectoryCountCache()
    request = _request(collection, "<Name>_<NumFiles>_<NumDirs>_<NumItems>", cache=cache)
    _files(tmp_path, "external.txt")
    (tmp_path / "external_folder").mkdir()
    original = preflight.evaluate_collection

    def evaluate(*args, **kwargs):
        assert kwargs["force_metadata_refresh"]
        return original(*args, **kwargs, metadata_cache=cache)

    monkeypatch.setattr(preflight, "evaluate_collection", evaluate)
    result = run_preflight(request)
    assert result.state is PreflightState.REVIEW_REQUIRED, result.diagnostic
    (change,) = result.candidate_changes
    assert change.previous_validated_filename == "One_1_0_1.txt"
    assert change.current_validated_filename == "One_2_1_1.txt"
    assert result.destination_changes[0].reviewed_path.endswith("One_1_0_1.txt")
    assert result.destination_changes[0].refreshed_path.endswith("One_2_1_1.txt")
    assert result.fresh_evaluation.directory_enumerations == 1
    assert collection.snapshot() == request.snapshot


@pytest.mark.parametrize(
    "mode", ["missing", "replacement", "same-size", "modified", "parent", "junction"]
)
def test_b14_to_b21_native_source_and_parent_changes(tmp_path, mode):
    root = tmp_path / "root"
    root.mkdir()
    (source,) = _files(root, "One.txt")
    request = _request(_collection((source,)))
    expected = PreflightState.SOURCE_CHANGED
    if mode == "missing":
        source.rename(root / "moved.txt")
    elif mode == "modified":
        source.write_bytes(b"changed")
    elif mode in {"replacement", "same-size"}:
        source.rename(root / "old.txt")
        source.write_bytes(b"test" if mode == "same-size" else b"replacement")
    else:
        expected = PreflightState.SAFETY_BLOCKED
        moved = root.rename(tmp_path / "moved")
        if mode == "parent":
            root.mkdir()
            (moved / source.name).rename(source)
        else:
            import _winapi

            _winapi.CreateJunction(str(moved), str(root))
    result = run_preflight(request)
    assert result.state is expected, result.diagnostic


def test_b18_absent_review_or_import_identity_is_unverifiable(tmp_path):
    paths = _files(tmp_path, "One.txt")
    for request in (
        _request(_collection(paths), weak=True),
        _request(_collection(paths, metadata=False)),
    ):
        result = run_preflight(request, observer_factory=_forbid_observation)
        assert result.state is PreflightState.UNVERIFIABLE


@pytest.mark.parametrize(
    "fault", ["mapping", "volume", "permission", "unsupported", "managed", "exception"]
)
def test_b22_b23_b25_b26_b41_observation_failures_are_terminal(tmp_path, fault):
    request = _request(_collection(_files(tmp_path, "One.txt")))
    original = request.reviewed_plan.entries[0].source_observation

    def factory(_options):
        def observe(_path):
            if fault == "permission":
                raise PermissionError(13, "denied")
            if fault == "exception":
                raise RuntimeError("test observer exception")
            if fault == "mapping":
                return replace(original, volume_guid="changed-volume")
            if fault == "managed":
                return PathObservation(PathOccupancy.UNSAFE, reason="managed_location")
            return PathObservation(PathOccupancy.UNVERIFIABLE, reason=fault)

        return observe

    expected = {
        "mapping": PreflightState.SAFETY_BLOCKED,
        "managed": PreflightState.SAFETY_BLOCKED,
        "exception": PreflightState.INTERNAL_FAILURE,
    }.get(fault, PreflightState.UNVERIFIABLE)
    assert run_preflight(request, observer_factory=factory).state is expected


def test_b19_reparse_entry_cannot_replace_source(tmp_path):
    import _winapi

    (source,) = _files(tmp_path, "One.txt")
    request = _request(_collection((source,)))
    source.rename(tmp_path / "retained.txt")
    target = tmp_path / "target"
    target.mkdir()
    _winapi.CreateJunction(str(target), str(source))
    assert run_preflight(request).state is PreflightState.SAFETY_BLOCKED


def test_b24_distinct_hard_link_entries_remain_distinct(tmp_path):
    (source,) = _files(tmp_path, "One.txt")
    other = tmp_path / "Two.txt"
    os.link(source, other)
    result = run_preflight(_request(_collection((source, other))))
    assert result.state is PreflightState.PASSED, result.diagnostic
    (_, first), (_, second) = result.source_observations
    assert first.object_metadata.identity == second.object_metadata.identity
    assert first.actual_name != second.actual_name


@pytest.mark.parametrize(
    ("names", "expression"),
    [
        (("01.txt", "02.txt", "03.txt"), "<IncNr:2>"),
        (("AB.txt", "BA.txt"), "<Move1:1>"),
        (("ABC.txt", "BCA.txt", "CAB.txt"), "<Move1:2>"),
    ],
)
def test_b28_b29_b30_dependencies_revalidated(tmp_path, names, expression):
    paths = _files(tmp_path, *names)
    request = _request(_collection(paths), expression)
    passed = run_preflight(request)
    assert passed.state is PreflightState.PASSED, passed.diagnostic
    assert passed.fresh_plan.dependency_groups == request.reviewed_plan.dependency_groups
    paths[1].rename(tmp_path / "moved.txt")
    assert run_preflight(request).state is PreflightState.SOURCE_CHANGED
    paths[1].write_bytes(b"test")
    assert run_preflight(request).diagnostic.code == "source_object_changed"


def test_b27_b31_native_new_collision_and_case_only_noop(tmp_path):
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    paths = (*_files(tmp_path, "First.txt"), *_files(other_dir, "first.txt"))
    collection = _collection(paths)
    name_plan = _request(collection, "<Name>")
    assert all(
        row.operation is PathOperation.NO_OP for row in run_preflight(name_plan).fresh_plan.entries
    )
    lower = _request(collection, "first")
    checked = run_preflight(lower)
    assert checked.state is PreflightState.PASSED, checked.diagnostic
    assert tuple(row.operation for row in checked.fresh_plan.entries) == (
        PathOperation.CASE_ONLY,
        PathOperation.NO_OP,
    )
    fresh = _request(collection)
    _files(tmp_path, "First better.txt")
    assert run_preflight(fresh).state is PreflightState.COLLISION_BLOCKED


def _subset(tmp_path):
    paths = _files(tmp_path, "00.txt", "01.txt", "02.txt", "QQ.txt")
    (occupant,) = _files(tmp_path, "03.txt")
    request = _request(_collection(paths), "<IncNr>")
    assert request.reviewed_plan.state is PlanState.COLLISION_BLOCKED
    return request, occupant


def test_b32_b33_b34_exact_subset_preserves_numbering(tmp_path):
    request, occupant = _subset(tmp_path)
    occupant.unlink()
    passed = run_preflight(request)
    assert passed.state is PreflightState.PASSED, passed.diagnostic
    assert passed.intended_entry_ids == request.intended_entry_ids
    (row,) = passed.fresh_plan.entries
    assert row.candidate_filename == "04.txt"
    assert len(passed.excluded_entry_ids) == 3
    _files(tmp_path, "04.txt")
    blocked = run_preflight(request)
    assert blocked.state is PreflightState.COLLISION_BLOCKED
    assert blocked.intended_entry_ids == request.intended_entry_ids


@pytest.mark.parametrize("bad_result", ["format", "request"])
def test_b06_b11_b35_fresh_batch_error_or_wrong_correlation(tmp_path, monkeypatch, bad_result):
    request, _ = _subset(tmp_path)
    original = preflight.evaluate_collection

    def evaluate(*args, **kwargs):
        fresh = original(*args, **kwargs)
        if bad_result == "request":
            return replace(fresh, request_id=uuid4())
        excluded = request.snapshot.entries[0]
        return replace(
            fresh,
            state=EvaluationState.INVALID,
            diagnostics=(
                EvaluationDiagnostic(
                    DiagnosticKind.TOKEN,
                    "simulated_token_error",
                    "refreshed file failed",
                    excluded.entry_id,
                ),
            ),
        )

    monkeypatch.setattr(preflight, "evaluate_collection", evaluate)
    state = PreflightState.FORMAT_BLOCKED if bad_result == "format" else PreflightState.STALE_INPUT
    assert run_preflight(request).state is state


def test_b36_incomplete_import_keeps_session_diagnostic(tmp_path):
    request = _request(_collection(_files(tmp_path, "One.txt"), incomplete=True))
    result = run_preflight(request, observer_factory=_forbid_observation)
    assert result.state is PreflightState.INCOMPLETE_IMPORT
    assert result.incomplete_imports == request.snapshot.incomplete_imports
    assert result.incomplete_imports[0].diagnostics[0].reason == "access_denied"


def test_acknowledged_incomplete_import_can_pass_preflight(tmp_path):
    collection = _collection(_files(tmp_path, "One.txt"), incomplete=True)
    snapshot = collection.snapshot()
    evaluation = evaluate_collection(snapshot, "<Name> renamed")
    acknowledged = tuple(session.session_id for session in snapshot.incomplete_imports)
    plan = capture_review_plan(
        snapshot,
        evaluation,
        current_request_id=evaluation.request_id,
        acknowledged_incomplete_import_ids=acknowledged,
    )
    request = PreflightRequest(
        snapshot,
        plan,
        evaluation,
        evaluation.request_id,
        "<Name> renamed",
        evaluation.settings,
        plan.options,
        tuple(entry.entry_id for entry in snapshot.entries),
        acknowledged_incomplete_import_ids=acknowledged,
    )

    result = run_preflight(request)

    assert result.state is PreflightState.PASSED
    assert result.acknowledged_incomplete_import_ids == acknowledged
    assert result.incomplete_imports == snapshot.incomplete_imports


@pytest.mark.parametrize("stage", [PreflightStage.METADATA, PreflightStage.DESTINATIONS])
def test_b37_b38_cancellation_during_read_only_preparation(tmp_path, stage):
    request = _request(_collection(_files(tmp_path, "One.txt")), "<Name>_<NumFiles>")
    cancelled = Event()

    def progress(update):
        if update.stage is stage:
            cancelled.set()

    result = run_preflight(request, cancel_check=cancelled.is_set, progress_callback=progress)
    assert result.state is PreflightState.CANCELLED


@pytest.mark.parametrize("fail_query", [False, True])
def test_b38_b40_cancel_or_fail_during_native_observation_releases_handles(tmp_path, fail_query):
    request = _request(_collection(_files(tmp_path, "One.txt")))
    cancelled = Event()
    opened, closed = [], []

    def factory(options):
        observer = WindowsPathObserver(options.managed_paths, capture_identity=True)
        native_open, native_close = observer._open, observer._close

        def open_handle(*args):
            handle = native_open(*args)
            opened.append(handle)
            return handle

        def close_handle(handle):
            closed.append(handle)
            return native_close(handle)

        observer._open, observer._close = open_handle, close_handle
        if fail_query:
            observer._file_info = lambda *_args: False

        def observe(path):
            actual = observer(path)
            if not fail_query:
                cancelled.set()
            return actual

        return observe

    result = run_preflight(request, observer_factory=factory, cancel_check=cancelled.is_set)
    expected = PreflightState.UNVERIFIABLE if fail_query else PreflightState.CANCELLED
    assert result.state is expected
    assert opened and sorted(opened) == sorted(closed)


def test_b39_bounded_service_supersedes_active_and_pending_requests(tmp_path):
    request = _request(_collection(_files(tmp_path, "One.txt")))
    entered, release = Event(), Event()
    caller = get_ident()

    def pause(update):
        assert get_ident() != caller
        if update.stage is PreflightStage.SOURCES:
            entered.set()
            assert release.wait(10)

    with PreflightService() as service:
        first = service.submit(request, progress_callback=pause)
        try:
            assert entered.wait(10)
            second = service.submit(replace(request, request_id=uuid4()))
            third = service.submit(replace(request, request_id=uuid4()))
            assert second.result(10).state is PreflightState.STALE_INPUT
            assert service.current_result() is None
        finally:
            release.set()
        assert first.result(10).state is PreflightState.STALE_INPUT
        latest = third.result(10)
        assert latest.state is PreflightState.PASSED
        assert service.current_result() is latest


def test_interstage_replacement_between_source_observation_and_destination_recheck(tmp_path):
    (source,) = _files(tmp_path, "One.txt")
    request = _request(_collection((source,)))

    def progress(update):
        if update.stage is PreflightStage.DESTINATIONS:
            source.rename(tmp_path / "original.txt")
            source.write_bytes(b"test")

    result = run_preflight(request, progress_callback=progress)
    assert result.state is PreflightState.SOURCE_CHANGED
    assert result.diagnostic.code == "source_object_changed"


def test_replaced_single_folder_destination_parent_is_blocked(tmp_path):
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (source,) = _files(nested, "One.txt")
    request = _request(
        _collection((source,), root=root), options=DestinationOptions(single_folder=True)
    )
    assert request.reviewed_plan.state is PlanState.VALID
    moved = root.rename(tmp_path / "original")
    root.mkdir()
    (moved / "nested").rename(nested)
    result = run_preflight(request)
    assert result.state is PreflightState.SAFETY_BLOCKED, result.diagnostic
    assert result.diagnostic.code == "destination_parent_or_volume_changed"


def test_b42_large_batch_probe_counts_and_determinism():
    count = 3000
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    files = {}
    parent = FileSystemIdentitySnapshot(1, 1)
    metadata = FileMetadataSnapshot(size_bytes=4, modified_time_ns=1, attributes=32)
    for index in range(count):
        path = rf"D:\Fixture\source-{index:05d}.txt"
        identity = FileSystemIdentitySnapshot(1, index + 2)
        collection.register_path(session, path, metadata=metadata, filesystem_identity=identity)
        files[path] = PathObservation(
            PathOccupancy.FILE,
            ntpath.basename(path),
            "volume",
            actual_parent=r"\Fixture",
            volume_guid="volume-guid",
            parent_identity=parent,
            object_metadata=WindowsObjectObservation(identity, 4, 1, None, 2, 32),
        )
    collection.finalize_import(session)
    vacant = PathObservation(
        PathOccupancy.VACANT,
        volume_key="volume",
        actual_parent=r"\Fixture",
        volume_guid="volume-guid",
        parent_identity=parent,
    )
    snapshot = collection.snapshot()
    evaluation = evaluate_collection(snapshot, "<Name> renamed")
    reviewed = capture_review_plan(
        snapshot,
        evaluation,
        current_request_id=evaluation.request_id,
        observe=lambda path: files.get(path, vacant),
    )
    request = PreflightRequest(
        snapshot,
        reviewed,
        evaluation,
        evaluation.request_id,
        "<Name> renamed",
        evaluation.settings,
        reviewed.options,
        tuple(entry.entry_id for entry in snapshot.entries),
    )
    counts = []

    def factory(_options):
        seen = set()
        counts.append(seen)

        def observe(path):
            assert path not in seen
            seen.add(path)
            return files.get(path, vacant)

        return observe

    first = run_preflight(request, observer_factory=factory)
    second = run_preflight(request, observer_factory=factory)
    assert first.state is PreflightState.PASSED
    assert first == second
    assert [len(seen) for seen in counts] == [count, count * 2] * 2
