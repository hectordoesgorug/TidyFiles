from dataclasses import FrozenInstanceError

import pytest

from tidyfiles.collection import (
    CollectionError,
    FileMetadataSnapshot,
    FileSystemIdentitySnapshot,
    ImportMode,
    ImportSessionClosedError,
    ImportSessionState,
    WorkingCollection,
    normalize_windows_path,
)


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
