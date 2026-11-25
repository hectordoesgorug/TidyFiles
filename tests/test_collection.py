from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from tidyfiles.collection import (
    CollectionError,
    FileMetadataSnapshot,
    FileSystemIdentitySnapshot,
    ImportDiagnostic,
    ImportMode,
    ImportSessionClosedError,
    ImportSessionState,
    OrderingMode,
    WorkingCollection,
    normalize_windows_path,
)
from tidyfiles.sorting import SortCriterion, SortDirection, SortPreferences


def test_windows_path_key_is_lexical_and_case_insensitive() -> None:
    assert normalize_windows_path(r"C:/Photos/Day/../A.JPG") == r"c:\photos\a.jpg"
    assert normalize_windows_path(r"\\server\share\folder\..\A.JPG") == (
        r"\\server\share\a.jpg"
    )


def test_overlapping_imports_keep_first_path_context_and_metadata() -> None:
    collection = WorkingCollection()
    first = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=r"C:\Photos")
    first_added = collection.register_path(
        first,
        r"C:\Photos\Day1\IMG.JPG",
        metadata=FileMetadataSnapshot(size_bytes=12),
        filesystem_identity=FileSystemIdentitySnapshot(1, 9),
    )
    collection.finalize_import(first)

    second = collection.begin_import(ImportMode.ADD_FILES)
    duplicate = collection.register_path(
        second,
        r"c:/PHOTOS/Day1/IMG.JPG",
        metadata=FileMetadataSnapshot(size_bytes=99),
        filesystem_identity=FileSystemIdentitySnapshot(2, 10),
    )
    assert duplicate.added is False
    assert duplicate.entry.entry_id == first_added.entry.entry_id
    assert duplicate.entry.import_root_path == r"C:\Photos"
    assert duplicate.entry.import_root_name == "Photos"
    assert duplicate.entry.metadata == FileMetadataSnapshot(size_bytes=12)
    assert duplicate.entry.filesystem_identity == FileSystemIdentitySnapshot(1, 9)
    assert collection.snapshot().import_sessions[-1].entry_ids == ()
    collection.cancel_import(second)


def test_add_files_uses_each_immediate_parent_without_folder_authorization() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    entry = collection.register_path(session, r"D:\Archive\Trip\one.tar.gz").entry
    assert entry.import_root_path == r"D:\Archive\Trip"
    assert entry.import_root_name == "Trip"
    assert entry.parent_path == r"D:\Archive\Trip"
    assert entry.parent_name == "Trip"
    assert entry.source_extension == ".gz"
    assert collection.snapshot().folder_roots == ()
    collection.finalize_import(session)


def test_add_folder_context_and_authorization_are_separate_from_file_parent() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=r"D:\Archive\Trip")
    entry = collection.register_path(
        session, r"D:\Archive\Trip\Day1\one.jpg"
    ).entry
    snapshot = collection.snapshot()
    assert entry.import_root_path == r"D:\Archive\Trip"
    assert entry.import_root_name == "Trip"
    assert entry.parent_path == r"D:\Archive\Trip\Day1"
    assert entry.parent_name == "Day1"
    assert len(snapshot.folder_roots) == 1
    assert snapshot.folder_roots[0].path == r"D:\Archive\Trip"
    assert snapshot.folder_roots[0].import_session_id == session
    collection.finalize_import(session)


def test_distinct_paths_are_not_merged_by_filesystem_identity() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    identity = FileSystemIdentitySnapshot("volume", "same-object")
    one = collection.register_path(session, r"C:\one.txt", filesystem_identity=identity)
    two = collection.register_path(session, r"C:\alias\two.txt", filesystem_identity=identity)
    assert one.added and two.added
    assert one.entry.entry_id != two.entry.entry_id
    assert one.entry.normalized_path_key != two.entry.normalized_path_key
    collection.finalize_import(session)


def test_reordering_preserves_entry_ids_and_existing_snapshots_are_immutable() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    first = collection.register_path(session, r"C:\one.txt").entry
    second = collection.register_path(session, r"C:\two.txt").entry
    collection.finalize_import(session)
    before = collection.snapshot()

    collection.set_order((second.entry_id, first.entry_id))
    after = collection.snapshot()
    assert after.collection_id == before.collection_id
    assert tuple(entry.entry_id for entry in before.entries) == (first.entry_id, second.entry_id)
    assert tuple(entry.entry_id for entry in after.entries) == (second.entry_id, first.entry_id)
    assert after.entries[0].entry_id == second.entry_id
    with pytest.raises(FrozenInstanceError):
        after.entries[0].source_path = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="every collection entry"):
        collection.set_order((first.entry_id, first.entry_id))


def test_cancel_rolls_back_only_current_session_and_restores_prior_order() -> None:
    collection = WorkingCollection()
    previous_session = collection.begin_import(
        ImportMode.ADD_FOLDER, folder_root=r"C:\Previous"
    )
    previous = collection.register_path(previous_session, r"C:\Previous\a.txt").entry
    other = collection.register_path(previous_session, r"C:\Previous\b.txt").entry
    collection.finalize_import(previous_session)
    collection.set_order((other.entry_id, previous.entry_id))
    prior = collection.snapshot()

    cancelled = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=r"D:\Temporary")
    pending = collection.register_path(cancelled, r"D:\Temporary\c.txt").entry
    assert collection.capabilities.is_importing
    assert not collection.capabilities.can_reorder
    with pytest.raises(CollectionError, match="while an import is active"):
        collection.set_order((pending.entry_id, other.entry_id, previous.entry_id))
    collection.cancel_import(cancelled)

    after = collection.snapshot()
    assert tuple(entry.entry_id for entry in after.entries) == tuple(
        entry.entry_id for entry in prior.entries
    )
    assert after.entries == prior.entries
    assert tuple(root.path for root in after.folder_roots) == (r"C:\Previous",)
    assert after.import_sessions[-1].state is ImportSessionState.CANCELLED
    assert pending.entry_id not in {entry.entry_id for entry in after.entries}
    assert after.ordering_mode is prior.ordering_mode is OrderingMode.MANUAL
    assert not collection.is_current(prior)


def test_manual_order_operations_preserve_selected_and_unselected_relative_order() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    entries = [collection.register_path(session, f"C:\\{name}.txt").entry for name in "ABCDE"]
    collection.finalize_import(session)
    ids = [entry.entry_id for entry in entries]

    assert collection.move_to_top((ids[1], ids[3]))
    assert [entry.entry_id for entry in collection.snapshot().entries] == [
        ids[1], ids[3], ids[0], ids[2], ids[4]
    ]
    collection.set_order(ids)
    assert collection.move_up((ids[1], ids[3]))
    assert [entry.entry_id for entry in collection.snapshot().entries] == [
        ids[1], ids[0], ids[3], ids[2], ids[4]
    ]
    collection.set_order(ids)
    assert collection.move_down((ids[1], ids[3]))
    assert [entry.entry_id for entry in collection.snapshot().entries] == [
        ids[0], ids[2], ids[1], ids[4], ids[3]
    ]
    collection.set_order(ids)
    assert collection.move_to_bottom((ids[1], ids[3]))
    assert [entry.entry_id for entry in collection.snapshot().entries] == [
        ids[0], ids[2], ids[4], ids[1], ids[3]
    ]
    assert collection.ordering_mode is OrderingMode.MANUAL


def test_manual_reorder_noops_and_stale_ids_do_not_change_revision() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    first = collection.register_path(session, r"C:\one.txt").entry
    collection.finalize_import(session)
    before = collection.snapshot()

    assert not collection.set_order((first.entry_id,))
    assert not collection.move_up(())
    assert not collection.move_up((first.entry_id,))
    assert not collection.move_to_bottom((first.entry_id,))
    assert not collection.move_to_top((uuid4(),))
    assert collection.snapshot().revision == before.revision
    assert collection.ordering_mode is OrderingMode.AUTOMATIC
    with pytest.raises(ValueError):
        collection.set_order((uuid4(),))


def test_manual_boundary_all_selection_and_unknown_ids_are_safe() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    entries = [collection.register_path(session, rf"C:\{name}.txt").entry for name in "ABC"]
    collection.finalize_import(session)
    ids = [entry.entry_id for entry in entries]
    baseline = collection.snapshot()

    assert not collection.move_up((ids[0], uuid4()))
    assert not collection.move_down((ids[-1],))
    assert not collection.move_to_top(ids)
    assert collection.snapshot().revision == baseline.revision
    assert collection.move_up((ids[1], uuid4()))
    assert [entry.entry_id for entry in collection.snapshot().entries] == [ids[1], ids[0], ids[2]]


def test_remove_clear_and_reimport_are_non_destructive(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"keep")
    collection = WorkingCollection(
        SortPreferences(SortCriterion.SIZE, SortDirection.DESCENDING)
    )
    session = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=tmp_path)
    collection.register_path(session, source)
    other_source = tmp_path / "other.txt"
    other_source.write_bytes(b"other")
    other_entry = collection.register_path(session, other_source).entry
    collection.finalize_import(session)
    collection.move_to_bottom((other_entry.entry_id,))
    before_bytes = source.read_bytes()
    before = collection.snapshot()

    removed = collection.remove_entries((other_entry.entry_id,))
    assert tuple(item.entry_id for item in removed) == (other_entry.entry_id,)
    assert collection.snapshot().folder_roots == before.folder_roots
    assert collection.snapshot().ordering_mode is OrderingMode.MANUAL
    assert not collection.is_current(before)
    assert source.read_bytes() == before_bytes

    reimport = collection.begin_import(ImportMode.ADD_FILES)
    new_entry = collection.register_path(reimport, other_source).entry
    assert new_entry.entry_id != other_entry.entry_id
    collection.finalize_import(reimport)
    collection.move_to_bottom((new_entry.entry_id,))
    preference = collection.sort_preferences
    assert collection.clear_all()
    cleared = collection.snapshot()
    assert cleared.entries == cleared.folder_roots == cleared.import_sessions == ()
    assert cleared.ordering_mode is OrderingMode.AUTOMATIC
    assert cleared.sort_preferences == preference
    assert not cleared.has_incomplete_imports
    assert source.read_bytes() == before_bytes
    assert other_source.read_bytes() == b"other"


def test_bulk_remove_preserves_relative_order_and_unknown_ids_are_noop() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    entries = [collection.register_path(session, rf"C:\{name}.txt").entry for name in "ABCDE"]
    collection.finalize_import(session)
    ids = [entry.entry_id for entry in entries]
    prior = collection.snapshot()

    assert collection.remove_entries((ids[1], uuid4(), ids[3])) == (
        prior.entries[1],
        prior.entries[3],
    )
    after = collection.snapshot()
    assert [entry.entry_id for entry in after.entries] == [ids[0], ids[2], ids[4]]
    assert not collection.is_current(prior)
    unchanged = collection.snapshot()
    assert collection.remove_entries((uuid4(),)) == ()
    assert collection.is_current(unchanged)


def test_management_actions_are_blocked_while_import_is_active() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    entry = collection.register_path(session, r"C:\one.txt").entry

    assert collection.capabilities.is_importing
    assert not collection.capabilities.can_reorder
    assert not collection.capabilities.can_remove
    assert not collection.capabilities.can_clear_all
    assert not collection.capabilities.can_sort
    with pytest.raises(CollectionError):
        collection.move_up((entry.entry_id,))
    with pytest.raises(CollectionError):
        collection.remove_entries((entry.entry_id,))
    with pytest.raises(CollectionError):
        collection.clear_all()
    with pytest.raises(CollectionError):
        collection.apply_sorting()
    collection.cancel_import(session)


def test_incomplete_warning_survives_removal_until_clear_all() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    entry = collection.register_path(session, r"C:\one.txt").entry
    collection.finalize_import(
        session, incomplete=True, diagnostics=(ImportDiagnostic(r"C:\denied", "access denied"),)
    )
    collection.remove_entries((entry.entry_id,))
    assert collection.snapshot().has_incomplete_imports
    assert collection.snapshot().incomplete_imports[0].diagnostics[0].path == r"C:\denied"


def test_cancel_preserves_prior_incomplete_warning_context_and_manual_order() -> None:
    collection = WorkingCollection()
    initial = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=r"C:\Known")
    first = collection.register_path(initial, r"C:\Known\a.txt").entry
    second = collection.register_path(initial, r"C:\Known\b.txt").entry
    collection.finalize_import(
        initial,
        incomplete=True,
        diagnostics=(ImportDiagnostic(r"C:\Known\locked", "access denied"),),
    )
    collection.set_order((second.entry_id, first.entry_id))
    prior = collection.snapshot()

    pending = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=r"D:\Temp")
    collection.register_path(pending, r"D:\Temp\new.txt")
    collection.cancel_import(pending)

    after = collection.snapshot()
    assert after.entries == prior.entries
    assert after.folder_roots == prior.folder_roots
    assert after.incomplete_imports == prior.incomplete_imports
    assert after.ordering_mode is prior.ordering_mode
    assert after.sort_preferences == prior.sort_preferences
    assert not collection.is_current(prior)


def test_finalization_and_late_session_results_are_rejected() -> None:
    collection = WorkingCollection()
    first = collection.begin_import(ImportMode.ADD_FILES)
    provisional = collection.register_path(first, r"C:\one.txt").entry
    assert collection.snapshot().finalized_entries == ()
    collection.finalize_import(first)
    assert collection.snapshot().finalized_entries[0].is_finalized
    with pytest.raises(ImportSessionClosedError):
        collection.register_path(first, r"C:\late.txt")

    second = collection.begin_import(ImportMode.ADD_FILES)
    assert second != first
    collection.cancel_import(second)
    with pytest.raises(ImportSessionClosedError):
        collection.register_path(second, r"C:\late-again.txt")
    assert collection.snapshot().entries == (collection.snapshot().finalized_entries[0],)
    assert provisional.import_session_id == first


def test_empty_collection_and_unavailable_metadata_are_explicit() -> None:
    empty = WorkingCollection().snapshot()
    assert empty.entries == ()
    assert empty.finalized_entries == ()

    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    entry = collection.register_path(session, r"C:\no-extension").entry
    assert entry.source_extension == ""
    assert entry.metadata is None
    assert entry.parent_identity == "c:\\"
    collection.finalize_import(session)
    assert collection.snapshot().entries[0].metadata is None


def test_model_operations_do_not_modify_source_files(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_bytes(b"keep this data")
    before = (source.read_bytes(), source.stat().st_size, source.stat().st_mtime_ns)

    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FOLDER, folder_root=tmp_path)
    entry = collection.register_path(session, source).entry
    collection.finalize_import(session)
    collection.set_order((entry.entry_id,))
    after = (source.read_bytes(), source.stat().st_size, source.stat().st_mtime_ns)

    assert after == before


def test_only_one_open_session_is_allowed() -> None:
    collection = WorkingCollection()
    collection.begin_import(ImportMode.ADD_FILES)
    with pytest.raises(CollectionError, match="already open"):
        collection.begin_import(ImportMode.ADD_FILES)


def test_prepared_import_order_sorts_then_finalizes_the_matching_revision() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    z_entry = collection.register_path(session, r"C:\Batch\z.txt").entry
    a_entry = collection.register_path(session, r"C:\Batch\a.txt").entry

    prepared = collection.prepare_import_order(session)
    assert prepared.entry_ids == (a_entry.entry_id, z_entry.entry_id)
    collection.finalize_import(session, prepared_order=prepared)
    snapshot = collection.snapshot()
    assert [entry.original_filename for entry in snapshot.entries] == ["a.txt", "z.txt"]
    assert all(entry.is_finalized for entry in snapshot.entries)


def test_prepared_import_order_rejects_a_stale_collection_revision() -> None:
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    collection.register_path(session, r"C:\Batch\z.txt")
    prepared = collection.prepare_import_order(session)
    collection.register_path(session, r"C:\Batch\a.txt")

    with pytest.raises(CollectionError, match="stale or incomplete"):
        collection.finalize_import(session, prepared_order=prepared)
    collection.finalize_import(session)


def test_prepared_manual_import_order_keeps_prior_entries_first() -> None:
    collection = WorkingCollection()
    first_session = collection.begin_import(ImportMode.ADD_FILES)
    first = collection.register_path(first_session, r"C:\Prior\b.txt").entry
    second = collection.register_path(first_session, r"C:\Prior\a.txt").entry
    collection.finalize_import(first_session)
    prior_order = (first.entry_id, second.entry_id)
    collection.set_order(prior_order)

    next_session = collection.begin_import(ImportMode.ADD_FILES)
    collection.register_path(next_session, r"C:\Next\z.txt")
    collection.register_path(next_session, r"C:\Next\a.txt")
    prepared = collection.prepare_import_order(next_session)
    assert prepared.entry_ids[:2] == prior_order

    collection.finalize_import(next_session, prepared_order=prepared)
    assert tuple(entry.entry_id for entry in collection.snapshot().entries) == prepared.entry_ids
