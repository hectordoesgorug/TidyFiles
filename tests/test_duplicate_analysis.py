from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest

import tidyfiles.duplicate_analysis as duplicate
from tidyfiles.collection import (
    FileMetadataSnapshot,
    FileSystemIdentitySnapshot,
    ImportMode,
    WorkingCollection,
)
from tidyfiles.duplicate_analysis import (
    DuplicateAnalysisStage,
    DuplicateAnalysisState,
    DuplicateAnalyzer,
    DuplicateGroup,
    DuplicateProgress,
    HashAlgorithm,
)


def _import(collection: WorkingCollection, *paths: Path) -> tuple[UUID, ...]:
    session = collection.begin_import(ImportMode.ADD_FILES)
    ids = []
    for path in paths:
        info = path.stat()
        entry = collection.register_path(
            session,
            path,
            metadata=FileMetadataSnapshot(
                info.st_size,
                info.st_mtime_ns,
                info.st_birthtime_ns,
                attributes=info.st_file_attributes,
            ),
            filesystem_identity=FileSystemIdentitySnapshot(info.st_dev, info.st_ino),
        ).entry
        ids.append(entry.entry_id)
    collection.finalize_import(session)
    return tuple(ids)


def _files(root: Path, *contents: bytes) -> tuple[Path, ...]:
    paths = tuple(root / f"{index:02}.bin" for index in range(len(contents)))
    for path, data in zip(paths, contents, strict=True):
        path.write_bytes(data)
    return paths


def _unexpected(*_args, **_kwargs):
    pytest.fail("Unexpected content operation")


@pytest.mark.parametrize("contents", [(), (b"a",), (b"a", b"bb", b"ccc")])
def test_empty_or_unique_sizes_skip_content_reads(tmp_path, monkeypatch, contents):
    collection = WorkingCollection()
    ids = _import(collection, *_files(tmp_path, *contents)) if contents else ()
    monkeypatch.setattr(duplicate, "_read_chunk", _unexpected)

    result = DuplicateAnalyzer().analyze(collection.snapshot())

    assert result.state is DuplicateAnalysisState.COMPLETE
    assert result.analyzed_ids == ids
    assert not result.groups and not result.failures


def test_same_size_different_hashes_skip_comparison(tmp_path, monkeypatch):
    collection = WorkingCollection()
    ids = _import(collection, *_files(tmp_path, b"ab", b"cd"))
    monkeypatch.setattr(duplicate, "_matches", _unexpected)

    result = DuplicateAnalyzer().analyze(collection.snapshot())

    assert not result.groups and not result.failures
    assert result.analyzed_ids == ids


@pytest.mark.parametrize("algorithm", list(HashAlgorithm))
def test_groups_match_across_names_and_extensions_and_leave_files_unchanged(tmp_path, algorithm):
    paths = (*_files(tmp_path, b"foo", b"bar", b"foo", b"bar"), tmp_path / "copy.txt")
    paths[-1].write_bytes(b"foo")
    collection = WorkingCollection()
    a, b, c, d, e = _import(collection, *paths)
    collection.set_order((e, b, a, d, c))
    snapshot = collection.snapshot()
    original = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}

    result = DuplicateAnalyzer().analyze(snapshot, algorithm=algorithm)

    assert result.groups == (DuplicateGroup(e, (a, c)), DuplicateGroup(b, (d,)))
    assert result.analyzed_ids == (e, b, a, d, c)
    assert not result.failures
    assert (result.collection_id, result.revision) == (snapshot.collection_id, snapshot.revision)
    assert collection.snapshot() == snapshot
    assert set(tmp_path.iterdir()) == set(paths)
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == original


@pytest.mark.parametrize("equal_copies", [False, True])
def test_forced_hash_collision_partitions_by_bytes(tmp_path, monkeypatch, equal_copies):
    collection = WorkingCollection()
    contents = (b"a", b"b", b"a", b"b") if equal_copies else (b"a", b"b")
    ids = _import(collection, *_files(tmp_path, *contents))
    monkeypatch.setattr(duplicate, "_hash_source", lambda *_: b"collision")
    engine = DuplicateAnalyzer()

    result = engine.analyze(collection.snapshot())

    expected = (
        (DuplicateGroup(ids[0], (ids[2],)), DuplicateGroup(ids[1], (ids[3],)))
        if equal_copies else ()
    )
    assert result.groups == expected
    assert result.analyzed_ids == ids
    assert not result.failures

    collection.set_order(ids[::-1])
    monkeypatch.setattr(duplicate, "_hash_source", _unexpected)
    monkeypatch.setattr(duplicate, "_matches", _unexpected)
    reordered = engine.analyze(collection.snapshot())
    expected = (
        (DuplicateGroup(ids[3], (ids[1],)), DuplicateGroup(ids[2], (ids[0],)))
        if equal_copies else ()
    )
    assert reordered.groups == expected


def test_worker_completion_order_does_not_select_keeper(tmp_path, monkeypatch):
    collection = WorkingCollection()
    a, b, c = _import(collection, *_files(tmp_path, b"a", b"a", b"a"))
    sequential = DuplicateAnalyzer().analyze(collection.snapshot(), workers=1)
    hash_source = duplicate._hash_source
    second_done = Event()
    hashed = []
    outstanding = []
    wait = duplicate.wait

    def finish_second_first(observer, source, algorithm, check):
        if source.entry_id == a:
            assert second_done.wait(5), "Second worker did not reach the barrier"
        result = hash_source(observer, source, algorithm, check)
        hashed.append(source.entry_id)
        if source.entry_id == b:
            second_done.set()
        return result

    monkeypatch.setattr(duplicate, "_hash_source", finish_second_first)

    def track_outstanding(futures, **kwargs):
        outstanding.append(len(futures))
        return wait(futures, **kwargs)

    monkeypatch.setattr(duplicate, "wait", track_outstanding)
    parallel = DuplicateAnalyzer().analyze(collection.snapshot(), workers=2)

    assert hashed.index(b) < hashed.index(a)
    assert parallel == sequential
    assert parallel.groups == (DuplicateGroup(a, (b, c)),)
    assert max(outstanding) == 2


def test_cumulative_import_hashes_new_candidates_and_reuses_unchanged_evidence(
    tmp_path, monkeypatch
):
    paths = _files(tmp_path, b"same", b"same", b"same")
    collection = WorkingCollection()
    a, = _import(collection, paths[0])
    engine = DuplicateAnalyzer()
    updates = []
    hashed: Counter[UUID] = Counter()
    original_hash = duplicate._hash_source

    def record_hash(observer, source, algorithm, check):
        hashed[source.entry_id] += 1
        return original_hash(observer, source, algorithm, check)

    monkeypatch.setattr(duplicate, "_hash_source", record_hash)
    assert not engine.analyze(collection.snapshot()).groups
    assert not hashed
    b, = _import(collection, paths[1])
    assert engine.analyze(collection.snapshot(), workers=1).groups == (DuplicateGroup(a, (b,)),)
    c, = _import(collection, paths[2])
    assert engine.analyze(
        collection.snapshot(), workers=1, progress_callback=updates.append
    ).groups == (DuplicateGroup(a, (b, c)),)
    assert hashed == {a: 1, b: 1, c: 1}
    assert [p for p in updates if p.stage is DuplicateAnalysisStage.HASHING] == [
        DuplicateProgress(DuplicateAnalysisStage.HASHING, 0, 1),
        DuplicateProgress(DuplicateAnalysisStage.HASHING, 1, 1),
    ]

    collection.remove_entries((a,))
    collection.set_order((c, b))
    monkeypatch.setattr(duplicate, "_hash_source", _unexpected)
    monkeypatch.setattr(duplicate, "_matches", _unexpected)
    result = engine.analyze(collection.snapshot())
    assert result.groups == (DuplicateGroup(c, (b,)),)
    assert result.analyzed_ids == (c, b)
    assert paths[0].read_bytes() == b"same"


@pytest.mark.parametrize("replace", [False, True])
def test_changed_or_replaced_source_cannot_reuse_prior_observations(tmp_path, replace):
    first, second = _files(tmp_path, b"aaaa", b"aaaa")
    collection = WorkingCollection()
    a, b = _import(collection, first, second)
    engine = DuplicateAnalyzer()
    assert engine.analyze(collection.snapshot()).groups == (DuplicateGroup(a, (b,)),)
    if replace:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"aaaa")
        replacement.replace(second)
    else:
        second.write_bytes(b"changed")

    result = engine.analyze(collection.snapshot())

    assert result.analyzed_ids == (a,)
    assert not result.groups
    assert len(result.failures) == 1
    assert result.failures[0].entry_id == b
    assert "changed since discovery" in result.failures[0].reason


def test_restored_mtime_does_not_hide_change_from_cache(tmp_path):
    first, second = _files(tmp_path, b"aaaa", b"aaaa")
    collection = WorkingCollection()
    a, b = _import(collection, first, second)
    engine = DuplicateAnalyzer()
    assert engine.analyze(collection.snapshot()).groups == (DuplicateGroup(a, (b,)),)
    baseline = second.stat()
    second.write_bytes(b"bbbb")
    os.utime(second, ns=(baseline.st_atime_ns, baseline.st_mtime_ns))

    result = engine.analyze(collection.snapshot())

    assert result.analyzed_ids == (a, b)
    assert not result.groups and not result.failures


def test_algorithm_change_rehashes_candidates(tmp_path, monkeypatch):
    collection = WorkingCollection()
    a, b = _import(collection, *_files(tmp_path, b"a", b"a"))
    engine = DuplicateAnalyzer()
    observed = []
    hash_source = duplicate._hash_source

    def record(observer, source, algorithm, check):
        digest = hash_source(observer, source, algorithm, check)
        observed.append((algorithm, len(digest)))
        return digest

    monkeypatch.setattr(duplicate, "_hash_source", record)
    assert engine.analyze(collection.snapshot(), workers=1).groups == (DuplicateGroup(a, (b,)),)
    assert engine.analyze(
        collection.snapshot(), workers=1, algorithm=HashAlgorithm.XXHASH64
    ).groups == (DuplicateGroup(a, (b,)),)
    assert observed == [(HashAlgorithm.XXHASH128, 16)] * 2 + [(HashAlgorithm.XXHASH64, 8)] * 2


def test_missing_and_inaccessible_files_are_isolated_including_unique_size(tmp_path, monkeypatch):
    paths = _files(tmp_path, b"missing", b"inaccessible", b"x", b"x")
    collection = WorkingCollection()
    a, b, c, d = _import(collection, *paths)
    paths[0].unlink()
    open_relative = duplicate._open_relative

    def deny(parent, name, access, share):
        if name == paths[1].name:
            return None, 0xC0000022  # STATUS_ACCESS_DENIED
        return open_relative(parent, name, access, share)

    monkeypatch.setattr(duplicate, "_open_relative", deny)
    result = DuplicateAnalyzer().analyze(collection.snapshot())

    assert result.groups == (DuplicateGroup(c, (d,)),)
    assert result.analyzed_ids == (c, d)
    assert [item.entry_id for item in result.failures] == [a, b]
    assert paths[0].name in result.failures[0].reason
    assert "PermissionError" in result.failures[1].reason


def test_hash_read_error_does_not_prevent_other_members_from_grouping(tmp_path, monkeypatch):
    collection = WorkingCollection()
    a, b, c = _import(collection, *_files(tmp_path, b"x", b"x", b"x"))
    original = duplicate._read_chunk

    def read(stream, source, check, exact_size=None):
        if source.entry_id == a:
            raise duplicate._FileFailure(a, OSError("injected hash read error"))
        return original(stream, source, check, exact_size)

    monkeypatch.setattr(duplicate, "_read_chunk", read)
    result = DuplicateAnalyzer().analyze(collection.snapshot())

    assert result.groups == (DuplicateGroup(b, (c,)),)
    assert result.analyzed_ids == (b, c)
    assert result.failures == (duplicate.DuplicateFailure(a, "OSError: injected hash read error"),)


@pytest.mark.parametrize(
    "phase", [DuplicateAnalysisStage.COMPARING, DuplicateAnalysisStage.REVALIDATING]
)
def test_change_during_analysis_excludes_only_changed_file(tmp_path, phase):
    first, second, third = _files(tmp_path, b"before", b"before", b"before")
    collection = WorkingCollection()
    a, b, c = _import(collection, first, second, third)

    def change(progress):
        if progress.stage is phase and progress.completed == 0:
            first.write_bytes(b"changed!")

    result = DuplicateAnalyzer().analyze(collection.snapshot(), progress_callback=change)

    assert result.groups == (DuplicateGroup(b, (c,)),)
    assert result.analyzed_ids == (b, c)
    assert [failure.entry_id for failure in result.failures] == [a]


@pytest.mark.parametrize("broken_index", [0, 1])
def test_comparison_io_failure_is_attributed_to_the_correct_side(
    tmp_path, monkeypatch, broken_index
):
    collection = WorkingCollection()
    ids = _import(collection, *_files(tmp_path, b"a", b"a", b"a"))
    original = duplicate._read_chunk

    def read(stream, source, check, exact_size=None):
        if exact_size is not None and source.entry_id == ids[broken_index]:
            raise duplicate._FileFailure(source.entry_id, OSError("injected read error"))
        return original(stream, source, check, exact_size)

    monkeypatch.setattr(duplicate, "_read_chunk", read)
    result = DuplicateAnalyzer().analyze(collection.snapshot())

    assert result.analyzed_ids == tuple(item for item in ids if item != ids[broken_index])
    assert result.groups == (DuplicateGroup(result.analyzed_ids[0], result.analyzed_ids[1:]),)
    assert [failure.entry_id for failure in result.failures] == [ids[broken_index]]


def test_zero_byte_files_are_exact_duplicates(tmp_path):
    collection = WorkingCollection()
    a, b, c = _import(collection, *_files(tmp_path, b"", b"", b"x"))

    result = DuplicateAnalyzer().analyze(collection.snapshot())

    assert result.groups == (DuplicateGroup(a, (b,)),)
    assert result.analyzed_ids == (a, b, c)
    assert not result.failures


@pytest.mark.parametrize("during_comparison", [False, True])
def test_cancel_during_reads_discards_partial_results_and_evidence(
    tmp_path, monkeypatch, during_comparison
):
    collection = WorkingCollection()
    ids = _import(collection, *_files(tmp_path, *(b"a",) * 10))
    engine = DuplicateAnalyzer()
    original = duplicate._read_chunk
    cancelled = Event()
    reads = []

    def cancel_after_read(stream, source, check, exact_size=None):
        data = original(stream, source, check, exact_size)
        if (exact_size is not None) == during_comparison:
            reads.append(source.entry_id)
            cancelled.set()
        return data

    monkeypatch.setattr(duplicate, "_read_chunk", cancel_after_read)
    updates = []
    result = engine.analyze(
        collection.snapshot(), workers=2, cancel_check=cancelled.is_set,
        progress_callback=updates.append,
    )

    assert result.state is DuplicateAnalysisState.CANCELLED
    assert not result.groups and not result.analyzed_ids and not result.failures
    assert 1 <= len(reads) <= (1 if during_comparison else 2)

    monkeypatch.setattr(duplicate, "_read_chunk", original)
    updates.clear()
    result = engine.analyze(collection.snapshot(), progress_callback=updates.append)
    assert result.groups == (DuplicateGroup(ids[0], ids[1:]),)
    assert DuplicateProgress(DuplicateAnalysisStage.HASHING, len(ids), len(ids)) in updates


def test_large_files_use_bounded_reads(tmp_path, monkeypatch):
    collection = WorkingCollection()
    paths = (tmp_path / "one.bin", tmp_path / "two.bin")
    size = 3 * duplicate._CHUNK_SIZE + 17
    for path in paths:
        with path.open("wb") as file:
            file.truncate(size)
    a, b = _import(collection, *paths)
    original = duplicate._read_chunk
    read_sizes = []

    def record(stream, source, check, exact_size=None):
        data = original(stream, source, check, exact_size)
        read_sizes.append((exact_size, len(data)))
        return data

    monkeypatch.setattr(duplicate, "_read_chunk", record)
    result = DuplicateAnalyzer().analyze(collection.snapshot())

    assert result.groups == (DuplicateGroup(a, (b,)),)
    assert max(length for _, length in read_sizes) == duplicate._CHUNK_SIZE
    assert sum(length for limit, length in read_sizes if limit is None) == 2 * size
    assert sum(length for limit, length in read_sizes if limit is not None) == 2 * size


def test_hard_links_remain_distinct_collection_paths(tmp_path):
    source, = _files(tmp_path, b"same file")
    alias = tmp_path / "alias"
    os.link(source, alias)
    collection = WorkingCollection()
    a, b = _import(collection, source, alias)
    collection.set_order((b, a))

    result = DuplicateAnalyzer().analyze(collection.snapshot())

    assert result.groups == (DuplicateGroup(b, (a,)),)
    assert os.path.samefile(source, alias)


def test_active_import_cannot_finalize_duplicate_analysis(tmp_path):
    collection = WorkingCollection()
    _import(collection, *_files(tmp_path, b"a", b"a"))
    collection.begin_import(ImportMode.ADD_FILES)

    with pytest.raises(ValueError, match="finalized collection"):
        DuplicateAnalyzer().analyze(collection.snapshot())
