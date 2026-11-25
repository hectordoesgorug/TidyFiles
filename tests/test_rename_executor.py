from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import pytest

import tidyfiles.rename_executor as executor_module
import tidyfiles.transaction_journal as journal_module
from tidyfiles.collection import (
    FileMetadataSnapshot,
    FileSystemIdentitySnapshot,
    ImportMode,
    WorkingCollection,
)
from tidyfiles.destination_observation import WindowsPathObserver
from tidyfiles.destination_planning import DestinationOptions
from tidyfiles.execution_preflight import (
    PreflightRequest,
    PreflightState,
    capture_review_plan,
    run_preflight,
)
from tidyfiles.filename_integration import EvaluationMode, EvaluationSettings, evaluate_collection
from tidyfiles.modifying_jobs import ModifyingJobCoordinator, ModifyingJobError
from tidyfiles.rename_executor import RenameExecutor, RenameOutcome, RenameScope, RenameState
from tidyfiles.transaction_journal import JournalStore, RecoveryStatus
from tidyfiles.transaction_record import OperationType, TransactionState
from tidyfiles.windows_rename import BoundRename

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows rename tests")


@pytest.fixture
def runtime(tmp_path):
    sources = tmp_path / "sources"
    portable = tmp_path / "portable"
    local = tmp_path / "local"
    sources.mkdir()
    portable.mkdir()
    local.mkdir()
    store = JournalStore(app_dir=portable, local_app_data=local)
    coordinator = ModifyingJobCoordinator()
    return sources, store, coordinator, RenameExecutor(coordinator, store)


def _files(folder, *names):
    paths = []
    for name in names:
        path = folder / name
        path.write_bytes(f"original {name}".encode())
        paths.append(path)
    return tuple(paths)


def _inventory(folder):
    return {path.name: (path.read_bytes(), path.stat().st_ino) for path in folder.iterdir()}


def _preflight(paths, expression, *, roots=None, settings=EvaluationSettings()):
    collection = WorkingCollection()
    import_sets = roots or ((None, paths),)
    by_path = {}
    for root, group in import_sets:
        mode = ImportMode.ADD_FOLDER if root else ImportMode.ADD_FILES
        batch = collection.begin_import(mode, folder_root=root)
        for path in group:
            info = path.lstat()
            entry = collection.register_path(
                batch,
                path,
                filesystem_identity=FileSystemIdentitySnapshot(info.st_dev, info.st_ino),
                metadata=FileMetadataSnapshot(
                    size_bytes=info.st_size,
                    modified_time_ns=info.st_mtime_ns,
                    created_time_ns=info.st_birthtime_ns,
                    attributes=info.st_file_attributes,
                ),
            ).entry
            by_path[path] = entry.entry_id
        collection.finalize_import(batch)
    collection.set_order(tuple(by_path[path] for path in paths))
    snapshot = collection.snapshot()
    options = DestinationOptions(single_folder=roots is not None)
    evaluation = evaluate_collection(snapshot, expression, settings=settings)
    plan = capture_review_plan(
        snapshot,
        evaluation,
        current_request_id=evaluation.request_id,
        options=options,
    )
    intended = plan.collision_subset_ids or tuple(row.entry_id for row in plan.entries)
    request = PreflightRequest(
        snapshot,
        plan,
        evaluation,
        evaluation.request_id,
        expression,
        settings,
        options,
        intended,
    )
    result = run_preflight(request)
    assert result.state is PreflightState.PASSED, result.diagnostic
    return result


def _execute(executor, result, *, progress=None):
    return executor.execute(
        result,
        authorized=RenameScope.from_review(result),
        current_preflight=lambda: result,
        progress=progress,
    )


def _assert_no_journals(store):
    assert not store.discover().transactions and not store.discover().storage_issues


@pytest.mark.parametrize(
    ("names", "expression", "expected", "operations", "stages"),
    [
        (("A.txt",), "Better", ("Better.txt",), ("A.txt",), 0),
        (("A.txt",), "<Name>", ("A.txt",), (), 0),
        (("A.txt",), "a", ("a.txt",), ("A.txt", "staged"), 1),
        (
            ("00.txt", "01.txt", "02.txt"),
            "<IncNr>",
            ("01.txt", "02.txt", "03.txt"),
            ("02.txt", "01.txt", "00.txt"),
            0,
        ),
        (
            ("AB.txt", "BA.txt"),
            "<Move1:1>",
            ("BA.txt", "AB.txt"),
            ("AB.txt", "BA.txt", "staged"),
            1,
        ),
        (
            ("ABC.txt", "BCA.txt", "CAB.txt"),
            "<Move1:2>",
            ("BCA.txt", "CAB.txt", "ABC.txt"),
            ("ABC.txt", "CAB.txt", "BCA.txt", "staged"),
            1,
        ),
    ],
    ids=["D01-rename", "D02-noop", "D03-case", "D04-chain", "D05-swap", "D06-cycle"],
)
def test_native_rename_group_strategies(runtime, names, expression, expected, operations, stages):
    root, store, coordinator, executor = runtime
    paths = _files(root, *names)
    before = _inventory(root)
    birth_times = [path.stat().st_birthtime_ns for path in paths]
    result = _preflight(paths, expression)
    moves = []
    staged = []

    def progress(update):
        if update.stage.startswith("before_"):
            moves.append(
                "staged"
                if update.stage == "before_install_staged"
                else Path(update.source_path).name
            )
        if update.stage == "before_staging":
            record = store.discover().transactions[0].locations[0].record
            pivot = next(row for row in record.entries if row.entry_id == update.entry_id)
            staged.append(pivot.staging_path)
            assert pivot.staging_path == update.destination_path
            assert not Path(pivot.staging_path).exists()

    done = _execute(executor, result, progress=progress)
    assert done.state is RenameState.SUCCEEDED, done
    assert done.transaction_id
    expected_outcome = RenameOutcome.RENAMED if operations else RenameOutcome.UNCHANGED
    assert all(row.outcome is expected_outcome for row in done.entries)
    assert tuple(row.actual_path for row in done.entries) == tuple(
        str(root / name) for name in expected
    )
    assert _inventory(root) == dict(zip(expected, (before[name] for name in names), strict=True))
    assert [(root / name).stat().st_birthtime_ns for name in expected] == birth_times
    assert tuple(moves) == operations
    assert len(staged) == stages and not any(Path(path).exists() for path in staged)
    assert not coordinator.busy and coordinator.normal_close_permitted
    _assert_no_journals(store)


def test_d07_d08_independent_single_folder_roots(runtime):
    root, store, _, executor = runtime
    first, child = root / "first", root / "first" / "nested"
    child.mkdir(parents=True)
    (source,) = _files(child, "One.txt")
    # Use a second test-owned root on the checkout volume, which can differ from TEMP.
    checkout = Path.cwd().resolve(strict=True)
    with TemporaryDirectory(prefix="tidyfiles-m3d-volume-", dir=checkout) as temporary:
        other = Path(temporary).resolve(strict=True)
        assert other.parent == checkout and other.name.startswith("tidyfiles-m3d-volume-")
        other_child = other / "nested"
        other_child.mkdir()
        (second,) = _files(other_child, "Two.txt")
        before = source.stat().st_ino, second.stat().st_ino
        result = _preflight(
            (source, second),
            "<Name> moved",
            roots=((first, (source,)), (other, (second,))),
        )
        done = _execute(executor, result)
        assert done.state is RenameState.SUCCEEDED, done
        targets = first / "One moved.txt", other / "Two moved.txt"
        assert tuple(path.stat().st_ino for path in targets) == before
        assert [path.read_bytes() for path in targets] == [b"original One.txt", b"original Two.txt"]
        assert not any(child.iterdir()) and not any(other_child.iterdir())
        plan = result.fresh_plan
        volumes = {row.source_observation.volume_guid for row in plan.entries}
        print(f"Independent fixture roots used {len(volumes)} physical volumes")
        _assert_no_journals(store)


def test_d09_collision_only_scope_preserves_original_numbering(runtime):
    root, store, _, executor = runtime
    included = _files(root, "00.txt", "01.txt", "02.txt", "QQ.txt")
    _files(root, "03.txt")
    before = _inventory(root)
    result = _preflight(included, "<IncNr>")
    assert result.excluded_entry_ids
    done = _execute(executor, result)
    assert done.state is RenameState.SUCCEEDED, done
    assert [Path(row.actual_path).name for row in done.entries] == ["04.txt"]
    before["04.txt"] = before.pop("QQ.txt")
    assert _inventory(root) == before
    _assert_no_journals(store)


@pytest.mark.parametrize(
    "failure",
    ["unauthorized", "stale", "cancelled", "failed", "conversion", "altered", "busy"],
)
def test_d10_d24_invalid_or_independently_busy_requests_are_rejected(runtime, failure):
    root, store, coordinator, executor = runtime
    paths = _files(root, "One.txt")
    before = _inventory(root)
    settings = (
        EvaluationSettings(EvaluationMode.RENAME_AND_REPACK, ".bin")
        if failure == "conversion"
        else EvaluationSettings()
    )
    result = _preflight(paths, "Better", settings=settings)
    approval = RenameScope.from_review(result)
    latest = result
    job = None
    if failure == "unauthorized":
        approval = None
    elif failure == "stale":
        latest = None
    elif failure == "cancelled":
        result = latest = replace(result, state=PreflightState.CANCELLED)
    elif failure == "failed":
        result = latest = replace(result, state=PreflightState.COLLISION_BLOCKED)
    elif failure == "altered":
        identifier, source, _ = approval.entries[0]
        approval = replace(approval, entries=((identifier, source, str(root / "Altered.txt")),))
    elif failure == "busy":
        job = coordinator.reserve(OperationType.TRASH)
    done = executor.execute(result, authorized=approval, current_preflight=lambda: latest)
    assert done.state is RenameState.REJECTED
    assert _inventory(root) == before and done.transaction_id is None
    assert coordinator.busy == (job is not None)
    if job is not None:
        coordinator.finish(job.job_id)
    _assert_no_journals(store)


@pytest.mark.parametrize("change", ["destination", "source", "root", "junction"])
def test_d11_d12_d13_external_changes_after_preflight_are_blocked(runtime, change):
    root, store, coordinator, executor = runtime
    (source,) = _files(root, "One.txt")
    result = _preflight((source,), "Better")
    if change == "destination":
        _files(root, "Better.txt")
    elif change == "source":
        source.rename(root / "retained.txt")
        source.write_bytes(b"replacement")
    else:
        old = root.rename(root.with_name("old"))
        if change == "root":
            root.mkdir()
            (old / source.name).rename(source)
        else:
            import _winapi

            _winapi.CreateJunction(str(old), str(root))
    expected = _inventory(root)
    done = _execute(executor, result)
    assert done.state is RenameState.FAILED_SAFE and done.transaction_id is None, done
    assert _inventory(root) == expected and not coordinator.busy
    _assert_no_journals(store)


def test_d11_kernel_nonoverwrite_after_last_vacancy_check(runtime, monkeypatch):
    root, store, _, executor = runtime
    (source,) = _files(root, "One.txt")
    original_identity = source.stat().st_ino
    result = _preflight((source,), "Better")
    check = BoundRename.check
    calls = 0

    def race(bound):
        nonlocal calls
        check(bound)
        calls += 1
        if calls == 2:
            _files(root, "Better.txt")

    monkeypatch.setattr(BoundRename, "check", race)
    done = _execute(executor, result)
    assert done.state is RenameState.FAILED_SAFE and calls == 2, done
    assert _inventory(root)["One.txt"] == (b"original One.txt", original_identity)
    assert (root / "Better.txt").read_bytes() == b"original Better.txt"
    _assert_no_journals(store)


def test_d13_target_parent_is_held_during_relocation(runtime):
    root, store, _, executor = runtime
    nested = root / "nested"
    nested.mkdir()
    (source,) = _files(nested, "One.txt")
    result = _preflight((source,), "Better", roots=((root, (source,)),))
    attempts = []

    def race(update):
        if update.stage == "before_rename":
            with pytest.raises(PermissionError):
                root.rename(root.with_name("replaced"))
            attempts.append(update.destination_path)

    done = _execute(executor, result, progress=race)
    assert done.state is RenameState.SUCCEEDED and attempts == [str(root / "Better.txt")], done
    assert (root / "Better.txt").read_bytes() == b"original One.txt"
    _assert_no_journals(store)


def test_d14_distinct_hard_link_entries_remain_distinct(runtime):
    root, store, _, executor = runtime
    (source,) = _files(root, "One.txt")
    other = root / "Two.txt"
    os.link(source, other)
    identity = source.stat().st_ino
    result = _preflight((source, other), "<Name> moved")
    done = _execute(executor, result)
    assert done.state is RenameState.SUCCEEDED, done
    assert _inventory(root) == {
        "One moved.txt": (b"original One.txt", identity),
        "Two moved.txt": (b"original One.txt", identity),
    }
    _assert_no_journals(store)


def test_d15_unowned_staging_path_is_preserved(runtime, monkeypatch):
    root, store, _, executor = runtime
    paths = _files(root, "AB.txt", "BA.txt")
    (stage,) = _files(root, ".tidyfiles-owned-looking.tmp")
    before = _inventory(root)
    result = _preflight(paths, "<Move1:1>")
    monkeypatch.setattr(executor_module, "_new_staging_path", lambda _source: str(stage))
    done = _execute(executor, result)
    assert done.state is RenameState.FAILED_SAFE, done
    assert _inventory(root) == before
    _assert_no_journals(store)


def test_d16_windows_sharing_lock_preserves_source(runtime):
    root, store, _, executor = runtime
    (source,) = _files(root, "One.txt")
    before = _inventory(root)
    result = _preflight((source,), "Better")
    native = WindowsPathObserver(capture_identity=True)
    handle = native._open(str(source), 0x80000000, 0x03, None, 3, 0, None)
    assert handle is not None and handle != -1 and handle != (1 << 64) - 1
    try:
        done = _execute(executor, result)
        assert done.state is RenameState.FAILED_SAFE, done
        assert _inventory(root) == before
    finally:
        native._close(handle)
    _assert_no_journals(store)


@pytest.mark.parametrize(
    "fault", ["journal", "locator", "before_move", "unexpected_before_move", "after_move"]
)
def test_d17_d18_d19_required_checkpoint_faults_stop_modification(runtime, monkeypatch, fault):
    root, store, coordinator, executor = runtime
    paths = _files(root, "One.txt", "Two.txt")
    before = _inventory(root)
    result = _preflight(paths, "<Name> new")
    publish = journal_module._publish

    def fail(temporary, destination, *, overwrite):
        data = json.loads(temporary.read_text(encoding="utf-8"))
        if destination == store.locator_path:
            broken = fault == "locator"
        else:
            stage = data["entries"][0]["stage"]
            broken = (
                (fault == "journal" and not overwrite)
                or (fault == "before_move" and stage == "before_rename")
                or (fault == "after_move" and stage == "installed")
            )
            if fault == "unexpected_before_move" and stage == "before_rename":
                raise RuntimeError("unexpected journal publication failure")
        if broken:
            raise OSError("injected journal publication failure")
        publish(temporary, destination, overwrite=overwrite)

    monkeypatch.setattr(journal_module, "_publish", fail)
    done = _execute(executor, result)
    assert done.state is (
        RenameState.RECOVERY_REQUIRED
        if fault in {"before_move", "unexpected_before_move", "after_move"}
        else RenameState.FAILED_SAFE
    ), done
    expected = before.copy()
    if fault == "after_move":
        expected["One new.txt"] = expected.pop("One.txt")
        assert done.entries[0].outcome is RenameOutcome.RENAMED
    assert _inventory(root) == expected and not coordinator.busy
    if fault != "journal":
        row = store.discover().transactions[0].locations[0].record.entries[0]
        assert row.stage == ("before_rename" if fault == "after_move" else "prepared")


def test_d20_failed_swap_keeps_verified_stage_available(runtime, monkeypatch):
    root, store, coordinator, executor = runtime
    paths = _files(root, "AB.txt", "BA.txt")
    before = _inventory(root)
    result = _preflight(paths, "<Move1:1>")
    move = BoundRename.move
    calls = 0
    checkpoints = []

    def fail(bound):
        nonlocal calls
        calls += 1
        if calls == 2:
            checkpoints.append(store.discover().transactions[0].locations[0].record)
            raise PermissionError("injected swap failure")
        move(bound)

    monkeypatch.setattr(BoundRename, "move", fail)
    done = _execute(executor, result)
    assert done.state is RenameState.RECOVERY_REQUIRED and not coordinator.busy, done
    journal = store.discover().transactions[0].locations[0]
    assert journal.status is RecoveryStatus.UNRESOLVED
    assert journal.record.state is TransactionState.RECOVERY_REQUIRED
    (before_failure,) = checkpoints
    assert before_failure.state is TransactionState.IN_PROGRESS
    assert [entry.stage for entry in before_failure.entries] == ["staged", "before_rename"]
    assert journal.record.entries == before_failure.entries
    assert journal.record.failure and "injected swap failure" in journal.record.failure
    stage = Path(journal.record.entries[0].staging_path)
    assert _inventory(root) == {"BA.txt": before["BA.txt"], stage.name: before["AB.txt"]}
    assert done.entries[0].actual_path == str(stage)
    assert done.entries[1].outcome is RenameOutcome.FAILED_SAFE


def test_d21_late_failure_retains_completed_and_untouched_files(runtime):
    root, store, _, executor = runtime
    paths = _files(root, "One.txt", "Two.txt", "Three.txt")
    before = _inventory(root)
    result = _preflight(paths, "<Name> moved")

    def interfere(update):
        if update.stage == "before_rename" and Path(update.source_path).name == "Two.txt":
            _files(root, "Two moved.txt")

    done = _execute(executor, result, progress=interfere)
    assert done.state is RenameState.FAILED_SAFE, done
    assert [row.outcome for row in done.entries] == [
        RenameOutcome.RENAMED,
        RenameOutcome.FAILED_SAFE,
        RenameOutcome.NOT_PROCESSED,
    ]
    after = _inventory(root)
    assert after["Two moved.txt"][0] == b"original Two moved.txt"
    del after["Two moved.txt"]
    before["One moved.txt"] = before.pop("One.txt")
    assert before == after
    _assert_no_journals(store)


def test_d22_unverified_final_state_retains_journal(runtime):
    root, store, coordinator, executor = runtime
    paths = _files(root, "One.txt")
    result = _preflight(paths, "Better")

    def interfere(update):
        if update.stage == "verifying":
            (root / "Better.txt").rename(root / "external.txt")

    done = _execute(executor, result, progress=interfere)
    assert done.state is RenameState.RECOVERY_REQUIRED and not coordinator.busy, done
    assert done.entries[0].outcome is RenameOutcome.RECOVERY_REQUIRED
    assert store.discover().transactions[0].locations[0].record.state is (
        TransactionState.RECOVERY_REQUIRED
    )
    assert (root / "external.txt").read_bytes() == b"original One.txt"


def test_d25_d26_cancellation_and_job_ownership(runtime):
    root, store, coordinator, executor = runtime
    paths = _files(root, "One.txt")
    before = _inventory(root)
    result = _preflight(paths, "Better")

    def cancel_early(update):
        assert coordinator.can_cancel and coordinator.normal_close_permitted
        assert coordinator.cancel_preparation(update.job_id)

    cancelled = _execute(executor, result, progress=cancel_early)
    assert cancelled.state is RenameState.CANCELLED and cancelled.transaction_id is None
    assert _inventory(root) == before and not coordinator.busy

    def cancel_late(update):
        if update.stage == "before_rename":
            assert not coordinator.normal_close_permitted
            assert not coordinator.cancel_preparation(update.job_id)
            with pytest.raises(ModifyingJobError):
                coordinator.reserve(OperationType.REPACK)

    done = _execute(executor, result, progress=cancel_late)
    assert done.state is RenameState.SUCCEEDED and not coordinator.busy, done
    assert _inventory(root) == {"Better.txt": before["One.txt"]}
    _assert_no_journals(store)


def test_supersession_during_journal_start_leaves_files_untouched(runtime):
    root, store, coordinator, executor = runtime
    paths = _files(root, "One.txt")
    before = _inventory(root)
    result = _preflight(paths, "Better")
    latest = result

    def supersede(update):
        nonlocal latest
        if update.stage == "modifying":
            latest = None

    done = executor.execute(
        result,
        authorized=RenameScope.from_review(result),
        current_preflight=lambda: latest,
        progress=supersede,
    )
    assert done.state is RenameState.REJECTED and not coordinator.busy, done
    assert _inventory(root) == before
    _assert_no_journals(store)


def test_cancellation_after_registration_retains_prepared_record(runtime, monkeypatch):
    root, store, coordinator, executor = runtime
    paths = _files(root, "One.txt")
    before = _inventory(root)
    result = _preflight(paths, "Better")
    prepare = store.prepare

    def cancel_after_prepare(record):
        journal = prepare(record)
        assert coordinator.cancel_preparation(coordinator.current.job_id)
        return journal

    monkeypatch.setattr(store, "prepare", cancel_after_prepare)
    done = _execute(executor, result)
    assert done.state is RenameState.CANCELLED and not coordinator.busy, done
    record = store.discover().transactions[0].locations[0].record
    assert (
        record.state is TransactionState.PREPARED and record.transaction_id == done.transaction_id
    )
    assert _inventory(root) == before


def test_external_artifact_at_consumed_stage_is_preserved(runtime):
    root, store, coordinator, executor = runtime
    paths = _files(root, "AB.txt", "BA.txt")
    before = _inventory(root)
    result = _preflight(paths, "<Move1:1>")
    staged = []

    def interfere(update):
        if update.stage == "before_staging":
            staged.append(Path(update.destination_path))
        if update.stage == "verifying":
            (stage,) = staged
            assert not stage.exists()
            stage.write_bytes(b"unrelated artifact")

    done = _execute(executor, result, progress=interfere)
    assert done.state is RenameState.RECOVERY_REQUIRED and not coordinator.busy, done
    (stage,) = staged
    after = _inventory(root)
    assert after.pop(stage.name)[0] == b"unrelated artifact"
    assert after == {"AB.txt": before["BA.txt"], "BA.txt": before["AB.txt"]}
    record = store.discover().transactions[0].locations[0].record
    assert record.entries[0].staging_path == str(stage)


def test_representative_bulk_rename(runtime):
    root, store, _, executor = runtime
    count = 100
    paths = _files(root, *(f"file-{index:03d}.txt" for index in range(count)))
    before = _inventory(root)
    preflight = _preflight(paths, "<Name> renamed")
    start = perf_counter()
    done = _execute(executor, preflight)
    elapsed = perf_counter() - start
    print(f"Native executor, {count} files including journal and verification: {elapsed:.3f}s")
    assert done.state is RenameState.SUCCEEDED and len(done.entries) == count, done
    assert _inventory(root) == {
        f"{Path(name).stem} renamed.txt": data for name, data in before.items()
    }
    _assert_no_journals(store)
