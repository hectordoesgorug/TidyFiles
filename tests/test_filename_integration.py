from __future__ import annotations

import os
import stat
import threading
from types import SimpleNamespace

import pytest

import tidyfiles.filename_integration as integration
from tidyfiles.collection import ImportMode, WorkingCollection
from tidyfiles.filename_evaluator import EvaluationCancelledError, evaluate_batch
from tidyfiles.filename_format import parse_format
from tidyfiles.filename_integration import (
    DiagnosticKind,
    DirectoryCountCache,
    EvaluationMode,
    EvaluationSettings,
    EvaluationStage,
    EvaluationState,
    FilenameEvaluationService,
    compare_candidate_filenames,
    evaluate_collection,
)
from tidyfiles.sorting import SortCriterion, SortDirection


def _add_files(collection: WorkingCollection, paths: list[os.PathLike[str] | str]) -> None:
    session = collection.begin_import(ImportMode.ADD_FILES)
    for path in paths:
        collection.register_path(session, path)
    collection.finalize_import(session)


def _result_by_path(result):
    return {item.entry.source_path: item for item in result.entries}


def _new_collection(path: os.PathLike[str] | str) -> WorkingCollection:
    collection = WorkingCollection()
    _add_files(collection, [path])
    return collection


def test_adapter_preserves_entry_identity_roots_and_actual_parents(tmp_path) -> None:
    root_a = tmp_path / "Vacation"
    day_a = root_a / "Day1"
    root_b = tmp_path / "Events"
    day_b = root_b / "Day1"
    day_a.mkdir(parents=True)
    day_b.mkdir(parents=True)
    path_a = day_a / "image01.png"
    path_b = day_b / "image02.jpg"
    path_a.write_bytes(b"a")
    path_b.write_bytes(b"b")

    collection = WorkingCollection()
    first = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=root_a)
    entry_a = collection.register_path(first, path_a).entry
    collection.finalize_import(first)
    second = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=root_b)
    entry_b = collection.register_path(second, path_b).entry
    collection.finalize_import(second)

    # Reimporting through Add Files cannot replace the established root context.
    duplicate = collection.begin_import(ImportMode.ADD_FILES)
    assert not collection.register_path(duplicate, path_a).added
    collection.cancel_import(duplicate)
    collection.set_order((entry_b.entry_id, entry_a.entry_id))

    snapshot = collection.snapshot()
    result = evaluate_collection(snapshot, "<DirName>_<SubDirName>_<Name>_<Ext>")
    rows = _result_by_path(result)

    assert result.state is EvaluationState.VALID
    assert rows[str(path_a)].entry.entry_id == entry_a.entry_id
    assert rows[str(path_a)].file_result.context.import_root_name == "Vacation"
    assert rows[str(path_a)].file_result.context.parent_name == "Day1"
    assert rows[str(path_a)].file_result.context.original_stem == "image01"
    assert rows[str(path_a)].validated_filename == "Vacation_Day1_image01_png.png"
    assert rows[str(path_b)].entry.entry_id == entry_b.entry_id
    assert rows[str(path_b)].file_result.context.import_root_name == "Events"
    assert rows[str(path_b)].validated_filename == "Events_Day1_image02_jpg.jpg"
    assert snapshot.entries == collection.snapshot().entries


def test_add_files_and_drive_root_directory_context_are_lexical() -> None:
    collection = WorkingCollection()
    _add_files(collection, [r"R:\image.jpg", r"D:\Photos\Day1\one.jpg"])

    result = evaluate_collection(collection.snapshot(), "<DirName>|<SubDirName>|<Name>|<Ext>")
    rows = {row.entry.source_path: row for row in result.entries}

    assert result.state is EvaluationState.VALID
    assert rows[r"R:\image.jpg"].file_result.context.import_root_name == ""
    assert rows[r"R:\image.jpg"].file_result.context.parent_name == ""
    assert rows[r"R:\image.jpg"].file_result.value == "||image|jpg"
    assert rows[r"D:\Photos\Day1\one.jpg"].file_result.context.import_root_name == "Day1"
    assert rows[r"D:\Photos\Day1\one.jpg"].file_result.context.parent_name == "Day1"


def test_counts_use_physical_immediate_contents_not_import_membership(tmp_path) -> None:
    photos = tmp_path / "Photos"
    new_folder = photos / "NewFolder"
    archive = photos / "Archive"
    new_folder.mkdir(parents=True)
    archive.mkdir()
    first = photos / "image01.jpg"
    first.write_bytes(b"1")
    (photos / "image02.jpg").write_bytes(b"2")
    (photos / "image03.jpg").write_bytes(b"3")
    (photos / "Notes.txt").write_bytes(b"notes")
    (photos / "Thumbs.db").write_bytes(b"clutter")
    nested = new_folder / "image04.jpg"
    nested.write_bytes(b"4")

    collection = WorkingCollection()
    _add_files(collection, [first, nested])
    original_bytes = first.read_bytes()
    cache = DirectoryCountCache()
    result = evaluate_collection(
        collection.snapshot(),
        "<NumFiles>_<NumDirs>_<NumItems>_<Name>",
        metadata_cache=cache,
    )
    rows = _result_by_path(result)

    assert result.state is EvaluationState.VALID
    assert result.distinct_parent_directories == 2
    assert result.directory_enumerations == 2
    assert rows[str(first)].metadata.physical_file_count == 5
    assert rows[str(first)].metadata.physical_directory_count == 2
    assert rows[str(nested)].metadata.physical_file_count == 1
    assert rows[str(nested)].metadata.physical_directory_count == 0
    assert rows[str(first)].validated_filename == "5_2_2_image01.jpg"
    assert rows[str(nested)].validated_filename == "1_0_2_image04.jpg"

    collection.remove_entries((rows[str(nested)].entry.entry_id,))
    after_removal = evaluate_collection(
        collection.snapshot(),
        "<NumFiles>_<NumDirs>_<NumItems>_<Name>",
        metadata_cache=cache,
    )
    remaining = after_removal.entries[0]
    assert remaining.metadata.physical_file_count == 5
    assert remaining.metadata.physical_directory_count == 2
    assert remaining.file_result.context.collection_size == 1
    assert remaining.validated_filename == "5_2_1_image01.jpg"
    assert after_removal.directory_enumerations == 0
    assert after_removal.directory_cache_hits == 1
    assert first.read_bytes() == original_bytes


def test_empty_directory_counts_are_available_zero_and_do_not_read_sources(
    tmp_path, monkeypatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    source = empty / "not-created.jpg"
    collection = _new_collection(source)
    cache = DirectoryCountCache()

    def forbidden_open(*_args, **_kwargs):
        raise AssertionError("directory count acquisition must not open file contents")

    monkeypatch.setattr("builtins.open", forbidden_open)
    result = evaluate_collection(
        collection.snapshot(), "<NumFiles>_<NumDirs>_<Name>", metadata_cache=cache
    )

    row = result.entries[0]
    assert result.state is EvaluationState.VALID
    assert row.metadata.available
    assert row.metadata.physical_file_count == 0
    assert row.metadata.physical_directory_count == 0
    assert row.validated_filename == "0_0_not-created.jpg"


def test_count_tokens_share_one_enumeration_and_simple_formats_skip_it(
    tmp_path, monkeypatch
) -> None:
    folder = tmp_path / "items"
    folder.mkdir()
    (folder / "one.jpg").write_bytes(b"1")
    collection = _new_collection(folder / "one.jpg")
    cache = DirectoryCountCache()
    scan = os.scandir
    scanned: list[str] = []

    def counted(path):
        scanned.append(os.path.abspath(path))
        return scan(path)

    monkeypatch.setattr(integration.os, "scandir", counted)
    counted_result = evaluate_collection(
        collection.snapshot(),
        "<NumFiles>_<NumFiles>_<NumDirs>_<NumDirs>_<Name>",
        metadata_cache=cache,
    )
    assert counted_result.state is EvaluationState.VALID
    assert counted_result.directory_enumerations == 1
    assert len(scanned) == 1

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a format without physical count tokens must not enumerate")

    cache.invalidate()
    monkeypatch.setattr(cache, "observe", forbidden)
    no_count = evaluate_collection(collection.snapshot(), "<Name>_<NumItems>", metadata_cache=cache)
    assert no_count.state is EvaluationState.VALID


def test_parse_errors_precede_metadata_and_keep_entry_association(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "items"
    folder.mkdir()
    collection = _new_collection(folder / "one.jpg")
    cache = DirectoryCountCache()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a malformed expression must fail before metadata acquisition")

    monkeypatch.setattr(cache, "observe", forbidden)
    result = evaluate_collection(collection.snapshot(), "<NumFiles", metadata_cache=cache)
    assert result.state is EvaluationState.INVALID
    assert result.entries[0].entry.entry_id == collection.snapshot().entries[0].entry_id
    assert result.entries[0].candidate_filename is None
    assert result.diagnostics[0].kind is DiagnosticKind.PARSE
    assert result.directory_enumerations == 0


@pytest.mark.parametrize(
    "expression",
    [
        "<Name>",
        "<Ext>",
        "<DirName>",
        "<SubDirName>",
        "<NumFiles>",
        "<NumDirs>",
        "<NumItems>",
        "<IncNr>",
        "<IncNrDir>",
        "<DecrNr>",
        "<IncAlpha>",
        "<Word>",
        "<Substr>",
        "<RSubstr>",
        "<RemoveL:1>",
        "<RemoveR:1>",
        "<Move1:1>",
        "<RepS:1,X>",
        "<RepM:1,1,X>",
        "<Ins0:X>",
    ],
)
def test_all_twenty_tokens_evaluate_through_collection_adapter(tmp_path, expression) -> None:
    folder = tmp_path / "tokens"
    folder.mkdir(exist_ok=True)
    path = folder / "Alpha Beta.jpg"
    path.write_bytes(b"source")
    collection = _new_collection(path)

    result = evaluate_collection(collection.snapshot(), expression)

    assert result.state is EvaluationState.VALID, result.diagnostics
    assert result.entries[0].entry.source_path == str(path)
    assert result.entries[0].is_valid
    if expression in {"<NumFiles>", "<NumDirs>"}:
        assert result.directory_enumerations == 1
    else:
        assert result.directory_enumerations == 0


def test_ordered_batch_numbering_uses_effective_collection_order(tmp_path) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    paths = [a / "image03.jpg", b / "image01.jpg", a / "image02.jpg", b / "image04.jpg"]
    collection = _new_collection(paths[0])
    _add_files(collection, [*paths[1:]])
    entries_by_path = {entry.source_path: entry for entry in collection.snapshot().entries}
    collection.set_order(tuple(entries_by_path[str(path)].entry_id for path in paths))

    result = evaluate_collection(collection.snapshot(), "<Name>_<IncNr>_<IncNrDir>")

    assert [row.validated_filename for row in result.entries] == [
        "image03_01_01.jpg",
        "image01_02_01.jpg",
        "image02_03_02.jpg",
        "image04_04_02.jpg",
    ]
    collection.set_order(
        tuple(reversed([entry.entry_id for entry in collection.snapshot().entries]))
    )
    reordered = evaluate_collection(collection.snapshot(), "<Name>_<IncNr>_<IncNrDir>")
    assert [row.file_result.context.global_index for row in reordered.entries] == [0, 1, 2, 3]
    assert [row.validated_filename for row in reordered.entries] == [
        "image04_01_01.jpg",
        "image02_02_01.jpg",
        "image01_03_02.jpg",
        "image03_04_02.jpg",
    ]


def test_automatic_and_explicit_sorting_order_are_the_numbering_authority(tmp_path) -> None:
    paths = [tmp_path / "z.jpg", tmp_path / "a.jpg", tmp_path / "m.jpg"]
    collection = WorkingCollection()
    _add_files(collection, paths)

    automatic = collection.snapshot()
    assert [entry.original_filename for entry in automatic.entries] == [
        "a.jpg",
        "m.jpg",
        "z.jpg",
    ]
    automatic_result = evaluate_collection(automatic, "<Name>_<IncNr>")
    assert [row.entry.entry_id for row in automatic_result.entries] == [
        entry.entry_id for entry in automatic.entries
    ]
    assert [row.validated_filename for row in automatic_result.entries] == [
        "a_01.jpg",
        "m_02.jpg",
        "z_03.jpg",
    ]

    collection.apply_sorting(SortCriterion.NAME, SortDirection.DESCENDING)
    explicitly_sorted = collection.snapshot()
    explicit_result = evaluate_collection(explicitly_sorted, "<Name>_<IncNr>")
    assert [row.validated_filename for row in explicit_result.entries] == [
        "z_01.jpg",
        "m_02.jpg",
        "a_03.jpg",
    ]


def test_per_directory_groups_use_normalized_parent_paths_not_basenames(tmp_path) -> None:
    first = tmp_path / "one" / "same"
    second = tmp_path / "two" / "same"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    paths = [first / "a.jpg", second / "b.jpg", first / "c.jpg", second / "d.jpg"]
    collection = WorkingCollection()
    _add_files(collection, paths)
    result = evaluate_collection(collection.snapshot(), "<IncNrDir>")

    assert [row.validated_filename for row in result.entries] == [
        "01.jpg",
        "01.jpg",
        "02.jpg",
        "02.jpg",
    ]


def test_one_invalid_file_invalidates_the_batch_and_keeps_good_rows(tmp_path) -> None:
    first = tmp_path / "First Good.jpg"
    second = tmp_path / "No.jpg"
    collection = WorkingCollection()
    _add_files(collection, [first, second])
    result = evaluate_collection(collection.snapshot(), "<Word:2>")

    assert result.state is EvaluationState.INVALID
    assert result.batch_result is not None and not result.batch_result.is_valid
    assert result.entries[0].validated_filename == "Good.jpg"
    assert result.entries[0].is_valid
    assert result.entries[1].candidate_filename is None
    assert result.entries[1].file_result.error.token == "<Word:2>"
    assert result.diagnostics[-1].entry_id == result.entries[1].entry.entry_id


def test_conversion_mode_uses_source_ext_tokens_and_intended_terminal_extension(tmp_path) -> None:
    path = tmp_path / "Vacation.jpg"
    collection = _new_collection(path)
    settings = EvaluationSettings(EvaluationMode.RENAME_AND_REPACK, intended_output_extension="png")

    embedded = evaluate_collection(collection.snapshot(), "<Name>_<Ext>", settings=settings)
    terminal = evaluate_collection(collection.snapshot(), "<Name>.<Ext>", settings=settings)
    ordinary = evaluate_collection(collection.snapshot(), "<Name>")

    assert embedded.entries[0].validated_filename == "Vacation_jpg.png"
    assert terminal.entries[0].validated_filename == "Vacation.png"
    assert ordinary.entries[0].validated_filename == "Vacation.jpg"
    changed = compare_candidate_filenames(ordinary, terminal)
    assert len(changed) == 1
    assert changed[0].entry_id == collection.snapshot().entries[0].entry_id
    assert (changed[0].previous_validated_filename, changed[0].current_validated_filename) == (
        "Vacation.jpg",
        "Vacation.png",
    )


def test_candidate_comparison_tracks_unvalidated_values_without_promoting_them(tmp_path) -> None:
    collection = _new_collection(tmp_path / "source.jpg")
    previous = evaluate_collection(collection.snapshot(), "CON")
    current = evaluate_collection(collection.snapshot(), "PRN")

    change = compare_candidate_filenames(previous, current)[0]

    assert previous.entries[0].candidate_filename == "CON.jpg"
    assert previous.entries[0].validated_filename is None
    assert current.entries[0].candidate_filename == "PRN.jpg"
    assert current.entries[0].validated_filename is None
    assert (change.previous_candidate, change.current_candidate) == ("CON.jpg", "PRN.jpg")
    assert (change.previous_validated_filename, change.current_validated_filename) == (
        None,
        None,
    )


def test_metadata_failure_is_per_entry_and_never_becomes_zero(tmp_path) -> None:
    good = tmp_path / "available"
    missing = tmp_path / "missing" / "unavailable.jpg"
    good.mkdir()
    collection = WorkingCollection()
    _add_files(collection, [good / "present.jpg", missing])
    result = evaluate_collection(collection.snapshot(), "<NumFiles>_<Name>")

    assert result.state is EvaluationState.INVALID
    assert result.entries[0].metadata.available
    assert result.entries[0].metadata.physical_file_count == 0
    assert result.entries[1].metadata.physical_file_count is None
    assert result.entries[1].metadata.failure.code == "missing_directory"
    metadata_error = next(
        item for item in result.diagnostics if item.kind is DiagnosticKind.METADATA
    )
    assert metadata_error.entry_id == result.entries[1].entry.entry_id
    assert metadata_error.required_tokens == ("<NumFiles>",)
    assert result.entries[0].is_valid
    assert not result.entries[1].is_valid


def test_partial_directory_enumeration_discards_partial_counts(tmp_path, monkeypatch) -> None:
    broken = tmp_path / "broken"
    intact = tmp_path / "intact"
    broken.mkdir()
    intact.mkdir()
    (broken / "one.txt").write_text("one", encoding="utf-8")
    (intact / "two.txt").write_text("two", encoding="utf-8")
    collection = WorkingCollection()
    _add_files(collection, [broken / "source.jpg", intact / "source.jpg"])
    real_scandir = os.scandir

    class PartialScan:
        def __init__(self, path: str) -> None:
            self._scan = real_scandir(path)
            self._yielded = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self._scan.close()

        def __iter__(self):
            return self

        def __next__(self):
            if self._yielded:
                raise PermissionError("simulated mid-enumeration denial")
            self._yielded = True
            return next(self._scan)

    def flaky(path):
        if os.path.abspath(path) == os.path.abspath(broken):
            return PartialScan(path)
        return real_scandir(path)

    monkeypatch.setattr(integration.os, "scandir", flaky)
    result = evaluate_collection(collection.snapshot(), "<NumFiles>_<Name>")
    rows = {row.entry.source_path: row for row in result.entries}

    assert result.state is EvaluationState.INVALID
    failed = rows[str(broken / "source.jpg")]
    assert failed.metadata.failure.code == "partial_enumeration"
    assert failed.metadata.physical_file_count is None
    assert rows[str(intact / "source.jpg")].metadata.physical_file_count == 1
    assert rows[str(intact / "source.jpg")].is_valid


def test_reparse_children_are_skipped_and_linked_parent_paths_fail(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "parent"
    folder.mkdir()
    (folder / "ordinary.txt").write_text("x", encoding="utf-8")
    collection = _new_collection(folder / "source.jpg")
    real_scandir = os.scandir

    class ReparseEntry:
        def stat(self, *, follow_symlinks: bool = True):
            assert follow_symlinks is False
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)

    class ExtraEntryScan:
        def __init__(self, path: str) -> None:
            self._scan = real_scandir(path)
            self._entries = iter([*self._scan, ReparseEntry()])

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self._scan.close()

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._entries)

    monkeypatch.setattr(integration.os, "scandir", ExtraEntryScan)
    result = evaluate_collection(collection.snapshot(), "<NumFiles>_<NumDirs>_<Name>")
    assert result.entries[0].metadata.physical_file_count == 1
    assert result.entries[0].metadata.physical_directory_count == 0

    target = tmp_path / "target"
    linked = tmp_path / "linked"
    target.mkdir()
    try:
        os.symlink(target, linked, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"native Windows directory symlink unavailable: {error}")
    linked_collection = _new_collection(linked / "source.jpg")
    linked_result = evaluate_collection(linked_collection.snapshot(), "<NumFiles>_<Name>")
    observation = linked_result.entries[0].metadata
    assert observation.failure.code == "linked_path"
    assert observation.physical_file_count is None


def test_non_symlink_reparse_parent_is_rejected_from_metadata(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "junction"
    folder.mkdir()
    collection = _new_collection(folder / "source.jpg")
    real_lstat = os.lstat
    fake_reparse = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=0x400,
        st_dev=1,
        st_ino=2,
    )

    def lstat(path, *args, **kwargs):
        if os.path.abspath(path) == os.path.abspath(folder):
            return fake_reparse
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(integration.os, "lstat", lstat)
    result = evaluate_collection(collection.snapshot(), "<NumFiles>")

    assert result.state is EvaluationState.INVALID
    assert result.entries[0].metadata.failure.code == "linked_path"
    assert result.entries[0].metadata.physical_file_count is None


def test_native_symlink_entries_are_not_counted(tmp_path) -> None:
    folder = tmp_path / "parent"
    target = tmp_path / "target.txt"
    folder.mkdir()
    target.write_text("target", encoding="utf-8")
    (folder / "real.txt").write_text("real", encoding="utf-8")
    (folder / "source.jpg").write_text("source", encoding="utf-8")
    try:
        os.symlink(target, folder / "alias.txt")
        os.symlink(folder, folder / "alias_directory", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"native Windows symlinks unavailable: {error}")
    result = evaluate_collection(
        _new_collection(folder / "source.jpg").snapshot(), "<NumFiles>_<NumDirs>"
    )
    observation = result.entries[0].metadata
    assert observation.physical_file_count == 2  # source fixture and its real file
    assert observation.physical_directory_count == 0


def test_hard_link_names_count_as_separate_physical_files(tmp_path) -> None:
    folder = tmp_path / "parent"
    folder.mkdir()
    original = folder / "one.txt"
    original.write_text("data", encoding="utf-8")
    (folder / "source.jpg").write_text("source", encoding="utf-8")
    try:
        os.link(original, folder / "two.txt")
    except OSError as error:
        pytest.skip(f"native hard links unavailable: {error}")
    result = evaluate_collection(_new_collection(folder / "source.jpg").snapshot(), "<NumFiles>")
    assert result.entries[0].metadata.physical_file_count == 3


def test_cache_invalidation_and_forced_refresh_bypass_success_and_failure(
    tmp_path, monkeypatch
) -> None:
    folder = tmp_path / "parent"
    folder.mkdir()
    collection = _new_collection(folder / "source.jpg")
    cache = DirectoryCountCache()
    first = cache.observe(folder)
    assert first.available and first.physical_file_count == 0
    (folder / "new.txt").write_text("new", encoding="utf-8")
    cached = cache.observe(folder)
    assert cached.cache_hit and cached.physical_file_count == 0
    refreshed = cache.refresh(folder)
    assert refreshed.available and not refreshed.cache_hit
    assert refreshed.physical_file_count == 1

    cache.invalidate(folder)
    invalidated = cache.observe(folder)
    assert invalidated.available and invalidated.physical_file_count == 1
    real_scandir = os.scandir

    def denied(path):
        if os.path.abspath(path) == os.path.abspath(folder):
            raise PermissionError("simulated permission failure")
        return real_scandir(path)

    monkeypatch.setattr(integration.os, "scandir", denied)
    failed_refresh = cache.refresh(folder)
    assert failed_refresh.failure.code == "inaccessible_directory"
    assert failed_refresh.physical_file_count is None

    monkeypatch.setattr(integration.os, "scandir", real_scandir)
    recovered = evaluate_collection(
        collection.snapshot(),
        "<NumFiles>",
        metadata_cache=cache,
        force_metadata_refresh=True,
    )
    assert recovered.state is EvaluationState.VALID
    assert recovered.entries[0].metadata.physical_file_count == 1
    assert recovered.directory_cache_hits == 0
    assert recovered.directory_enumerations == 1


def test_cache_key_detects_replaced_directory_object(tmp_path) -> None:
    folder = tmp_path / "parent"
    moved = tmp_path / "old-parent"
    folder.mkdir()
    cache = DirectoryCountCache()
    first = cache.observe(folder)
    (folder / "old.txt").write_text("old", encoding="utf-8")
    os.rename(folder, moved)
    folder.mkdir()
    (folder / "new.txt").write_text("new", encoding="utf-8")

    second = cache.observe(folder)

    assert first.filesystem_identity is not None
    assert second.filesystem_identity is not None
    assert first.filesystem_identity != second.filesystem_identity
    assert not second.cache_hit
    assert second.physical_file_count == 1


def test_replacement_during_enumeration_makes_counts_unavailable(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "parent"
    folder.mkdir()
    cache = DirectoryCountCache()
    real_inspect = integration._inspect_directory
    calls = 0

    def changing(path: str):
        nonlocal calls
        actual, _identity = real_inspect(path)
        calls += 1
        identity = integration.FileSystemIdentitySnapshot("v", calls)
        return actual, identity

    monkeypatch.setattr(integration, "_inspect_directory", changing)
    observation = cache.observe(folder)
    assert observation.failure.code == "directory_replaced_during_enumeration"
    assert observation.physical_file_count is None


def test_lexical_path_normalization_never_resolves_links(tmp_path, monkeypatch) -> None:
    folder = tmp_path / "parent"
    folder.mkdir()
    collection = _new_collection(folder / "one.jpg")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the collection adapter must not resolve a path")

    monkeypatch.setattr(os.path, "realpath", forbidden)
    result = evaluate_collection(collection.snapshot(), "<Name>_<NumItems>")
    assert result.state is EvaluationState.VALID
    assert result.directory_enumerations == 0


def test_active_import_snapshot_cannot_produce_a_current_evaluation(tmp_path) -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    collection.register_path(session, tmp_path / "pending.jpg")

    result = evaluate_collection(collection.snapshot(), "<Name>")

    assert result.state is EvaluationState.INVALID
    assert result.diagnostics[0].code == "collection_import_active"
    assert result.entries[0].candidate_filename is None


def test_batch_evaluator_cancels_cooperatively_during_file_processing() -> None:
    contexts = tuple(
        integration.FileEvaluationContext(f"file{index}", source_extension="jpg")
        for index in range(10)
    )
    cancelled = threading.Event()

    def progress(completed: int, _total: int) -> None:
        if completed == 2:
            cancelled.set()

    with pytest.raises(EvaluationCancelledError):
        evaluate_batch(
            parse_format("<Name>"),
            contexts,
            cancel_check=cancelled.is_set,
            progress_callback=progress,
        )


def test_async_service_reports_progress_currency_and_failed_then_successful_runs(tmp_path) -> None:
    folder = tmp_path / "parent"
    folder.mkdir()
    path = folder / "one.jpg"
    path.write_text("content", encoding="utf-8")
    collection = _new_collection(path)
    updates = []

    with FilenameEvaluationService() as service:
        invalid = service.submit(collection.snapshot(), "<Unknown>").result(timeout=5)
        assert invalid.state is EvaluationState.INVALID
        assert service.current_result(collection.snapshot()) == invalid

        future = service.submit(
            collection.snapshot(),
            "<NumFiles>_<Name>",
            progress_callback=updates.append,
        )
        result = future.result(timeout=5)
        assert result.state is EvaluationState.VALID
        assert future.request_id == result.request_id
        assert service.worker_count == 1
        assert service.pending_limit == 1
        assert service.lifecycle(collection.snapshot()) is EvaluationState.VALID
        assert service.is_current(result, collection.snapshot())
        assert {update.stage for update in updates} >= {
            EvaluationStage.PARSING,
            EvaluationStage.METADATA,
            EvaluationStage.CONTEXTS,
            EvaluationStage.EVALUATING,
        }
        assert service.current_result(collection.snapshot()) == result

        corrected = service.submit(collection.snapshot(), "<Name>").result(timeout=5)
        assert corrected.state is EvaluationState.VALID
        assert corrected.request_id != result.request_id
        assert service.current_result(collection.snapshot()) == corrected


def test_async_service_supersedes_slow_expression_and_output_extension(
    tmp_path, monkeypatch
) -> None:
    collection = _new_collection(tmp_path / "one.jpg")
    snapshot = collection.snapshot()
    started = threading.Event()
    release = threading.Event()
    original = integration.evaluate_collection

    def blocked(*args, **kwargs):
        if args[1] == "<Name>":
            started.set()
            assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(integration, "evaluate_collection", blocked)
    settings = EvaluationSettings(EvaluationMode.RENAME_AND_REPACK, intended_output_extension="png")
    with FilenameEvaluationService() as service:
        old = service.submit(snapshot, "<Name>")
        assert started.wait(timeout=5)
        new = service.submit(snapshot, "<Name>", settings=settings)
        release.set()
        old_result = old.result(timeout=5)
        new_result = new.result(timeout=5)

        assert old_result.state is EvaluationState.STALE
        assert old_result.entries == ()
        assert new_result.state is EvaluationState.VALID
        assert new_result.entries[0].validated_filename == "one.png"
        assert (
            service.current_result(snapshot, expression="<Name>", settings=settings) == new_result
        )
        assert service.is_current(old_result, snapshot) is False


def test_async_pending_cancellation_is_bounded_and_does_not_mutate_collection(
    tmp_path, monkeypatch
) -> None:
    collection = WorkingCollection()
    _add_files(collection, [tmp_path / "one.jpg", tmp_path / "two.jpg"])
    snapshot = collection.snapshot()
    order = tuple(entry.entry_id for entry in snapshot.entries)
    started = threading.Event()
    release = threading.Event()
    original = integration.evaluate_collection

    def blocked(*args, **kwargs):
        if args[1] == "first":
            started.set()
            assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(integration, "evaluate_collection", blocked)
    with FilenameEvaluationService() as service:
        active = service.submit(snapshot, "first")
        assert started.wait(timeout=5)
        displaced = service.submit(snapshot, "second")
        pending = service.submit(snapshot, "third")
        assert displaced.result(timeout=5).state is EvaluationState.STALE
        assert service.cancel(pending.request_id)
        cancelled = pending.result(timeout=5)
        release.set()
        assert active.result(timeout=5).state is EvaluationState.STALE
        assert cancelled.state is EvaluationState.CANCELLED
        assert collection.snapshot().entries == snapshot.entries
        assert tuple(entry.entry_id for entry in collection.snapshot().entries) == order
        assert service.lifecycle(snapshot) is EvaluationState.CANCELLED


def test_async_metadata_cancellation_and_collection_revision_staleness(
    tmp_path, monkeypatch
) -> None:
    folder = tmp_path / "parent"
    folder.mkdir()
    collection = _new_collection(folder / "one.jpg")
    original_scandir = os.scandir
    started = threading.Event()
    release = threading.Event()

    def blocked_scandir(path):
        if os.path.abspath(path) == os.path.abspath(folder):
            started.set()
            assert release.wait(timeout=5)
        return original_scandir(path)

    monkeypatch.setattr(integration.os, "scandir", blocked_scandir)
    with FilenameEvaluationService() as service:
        future = service.submit(collection.snapshot(), "<NumFiles>")
        assert started.wait(timeout=5)
        assert service.cancel(future.request_id)
        release.set()
        assert future.result(timeout=5).state is EvaluationState.CANCELLED

    monkeypatch.setattr(integration.os, "scandir", original_scandir)
    collection = WorkingCollection()
    _add_files(collection, [tmp_path / "a.jpg", tmp_path / "b.jpg"])
    before = collection.snapshot()
    started.clear()
    release.clear()

    original_evaluate = integration.evaluate_collection

    def blocking_evaluate(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(integration, "evaluate_collection", blocking_evaluate)
    with FilenameEvaluationService() as service:
        future = service.submit(before, "<Name>")
        assert started.wait(timeout=5)
        collection.set_order(tuple(reversed([entry.entry_id for entry in before.entries])))
        release.set()
        old_result = future.result(timeout=5)
        current = collection.snapshot()
        assert old_result.state is EvaluationState.VALID
        assert service.lifecycle(current) is EvaluationState.STALE
        assert service.current_result(current) is None
        assert not service.is_current(old_result, current)


def test_current_result_requires_current_expression_and_settings(tmp_path) -> None:
    collection = _new_collection(tmp_path / "one.jpg")
    snapshot = collection.snapshot()
    original_settings = EvaluationSettings()
    changed_settings = EvaluationSettings(
        EvaluationMode.RENAME_AND_REPACK, intended_output_extension="png"
    )
    with FilenameEvaluationService() as service:
        result = service.submit(snapshot, "<Name>", settings=original_settings).result(timeout=5)

        assert (
            service.current_result(snapshot, expression="<Name>", settings=original_settings)
            == result
        )
        assert (
            service.current_result(
                snapshot, expression="<Name>_changed", settings=original_settings
            )
            is None
        )
        assert (
            service.lifecycle(snapshot, expression="<Name>", settings=changed_settings)
            is EvaluationState.STALE
        )
        service.invalidate_metadata()
        assert service.current_result(snapshot) is None
        assert service.lifecycle(snapshot) is EvaluationState.STALE


def test_forced_metadata_refresh_changes_candidates_and_cache_is_bounded(tmp_path) -> None:
    folder = tmp_path / "parent"
    folder.mkdir()
    path = folder / "one.jpg"
    path.write_text("original", encoding="utf-8")
    collection = _new_collection(path)
    cache = DirectoryCountCache(max_entries=1)
    first = evaluate_collection(collection.snapshot(), "<NumFiles>_<Name>", metadata_cache=cache)
    (folder / "extra.txt").write_text("extra", encoding="utf-8")
    stale = evaluate_collection(collection.snapshot(), "<NumFiles>_<Name>", metadata_cache=cache)
    fresh = evaluate_collection(
        collection.snapshot(),
        "<NumFiles>_<Name>",
        metadata_cache=cache,
        force_metadata_refresh=True,
    )

    assert first.entries[0].validated_filename == "1_one.jpg"
    assert stale.entries[0].validated_filename == "1_one.jpg"
    assert stale.directory_cache_hits == 1
    assert fresh.entries[0].validated_filename == "2_one.jpg"
    assert fresh.directory_enumerations == 1
    assert compare_candidate_filenames(stale, fresh)[0].previous_validated_filename == "1_one.jpg"


def test_incomplete_import_warnings_remain_in_snapshot_during_evaluation(tmp_path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=folder)
    collection.register_path(session, folder / "one.jpg")
    collection.finalize_import(session, incomplete=True, diagnostics=[])
    snapshot = collection.snapshot()

    result = evaluate_collection(snapshot, "<Name>")

    assert result.state is EvaluationState.VALID
    assert snapshot.has_incomplete_imports
    assert collection.snapshot().incomplete_imports == snapshot.incomplete_imports
