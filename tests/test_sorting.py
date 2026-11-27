from __future__ import annotations

import tidyfiles.sorting as sorting
from tidyfiles.collection import FileMetadataSnapshot, ImportMode, WorkingCollection
from tidyfiles.filename_integration import evaluate_collection
from tidyfiles.sorting import (
    SortCriterion,
    SortDirection,
    natural_sort_key,
)


def _collection(rows):
    collection = WorkingCollection()
    session = collection.begin_import(ImportMode.ADD_FILES)
    entries = {}
    for path, metadata in rows:
        entries[path] = collection.register_path(
            session, path, metadata=metadata
        ).entry
    collection.finalize_import(session)
    return collection, entries


def _names(collection):
    return [entry.original_filename for entry in collection.snapshot().entries]


def test_each_sort_computes_natural_name_and_path_once_per_entry(monkeypatch) -> None:
    path = r"C:\files\Image 10.jpg"
    collection, _ = _collection([(path, None)])
    entries = collection.snapshot().entries
    calls = []

    def record_key(value):
        calls.append(value)
        return natural_sort_key(value)

    monkeypatch.setattr(sorting, "natural_sort_key", record_key)
    for criterion in SortCriterion:
        calls.clear()
        assert sorting.sort_entries(entries, sorting.SortPreferences(criterion)) == entries
        assert calls == ["Image 10.jpg", path]


def test_default_file_type_sort_matches_dd094_sample() -> None:
    names = ["243.mp3", "f.jpeg", "123.gif", "a.mp4", "b.jpeg"]
    collection, _ = _collection([(rf"C:\files\{name}", None) for name in names])
    assert _names(collection) == ["b.jpeg", "f.jpeg", "123.gif", "243.mp3", "a.mp4"]
    preview = evaluate_collection(collection.snapshot(), "<Name> <IncNr>")
    assert [row.validated_filename for row in preview.entries] == [
        "b 01.jpeg", "f 02.jpeg", "123 03.gif", "243 04.mp3", "a 05.mp4",
    ]
    collection.apply_sorting(direction=SortDirection.DESCENDING)
    preview = evaluate_collection(collection.snapshot(), "<Name> <IncNr>")
    assert [row.validated_filename for row in preview.entries] == [
        "a 01.mp4", "243 02.mp3", "123 03.gif", "f 04.jpeg", "b 05.jpeg",
    ]


def test_default_sort_groups_mixed_extensions_and_sorts_naturally() -> None:
    collection, _ = _collection(
        [
            (r"C:\files\Other 1.bin", None),
            (r"C:\files\Image 10.jpg", None),
            (r"C:\files\image 3.PNG", None),
            (r"C:\files\Animation 12.GIF", None),
            (r"C:\files\animation 2.gif", None),
            (r"C:\files\Video 1.mp4", None),
            (r"C:\files\Document 1.pdf", None),
            (r"C:\files\Audio 10.mp3", None),
            (r"C:\files\audio 2.WAV", None),
            (r"C:\files\Archive 1.zip", None),
            (r"C:\files\Image 2.JPG", None),
        ]
    )

    assert _names(collection) == [
        "Image 2.JPG",
        "image 3.PNG",
        "Image 10.jpg",
        "animation 2.gif",
        "Animation 12.GIF",
        "audio 2.WAV",
        "Audio 10.mp3",
        "Video 1.mp4",
        "Archive 1.zip",
        "Document 1.pdf",
        "Other 1.bin",
    ]


def test_descending_reverses_categories_and_natural_name_order() -> None:
    rows = [
        (r"C:\x\other.unknown", None),
        (r"C:\x\archive.bz2", None),
        (r"C:\x\audio.m4a", None),
        (r"C:\x\document.csv", None),
        (r"C:\x\video.ogv", None),
        (r"C:\x\Video 12.MP4", None),
        (r"C:\x\video 2.webm", None),
        (r"C:\x\anim.gif", None),
        (r"C:\x\image.svg", None),
    ]
    collection, _ = _collection(rows)

    ascending = [
        "image.svg",
        "anim.gif",
        "audio.m4a",
        "video 2.webm",
        "Video 12.MP4",
        "video.ogv",
        "archive.bz2",
        "document.csv",
        "other.unknown",
    ]
    assert _names(collection) == ascending
    collection.apply_sorting(direction=SortDirection.DESCENDING)
    assert _names(collection) == ascending[::-1]


def test_natural_sort_padded_numbers_and_multiple_components_are_deterministic() -> None:
    values = [
        "File 20.jpg",
        "File 1.jpg",
        "File 11.jpg",
        "File 10.jpg",
        "File 2.jpg",
        "File 3.jpg",
    ]
    assert sorted(values, key=natural_sort_key) == [
        "File 1.jpg",
        "File 2.jpg",
        "File 3.jpg",
        "File 10.jpg",
        "File 11.jpg",
        "File 20.jpg",
    ]

    collection, _ = _collection(
        [
            (rf"C:\x\{name}", None)
            for name in (
                "Image 1.png",
                "Image 01.png",
                "Image 001.png",
                "Image 2.png",
                "Image 10.png",
                "Chapter 10 Page 1.jpg",
                "Chapter 2 Page 10.jpg",
                "Chapter 2 Page 9.jpg",
                "Chapter 2 Page 1.jpg",
            )
        ]
    )
    assert _names(collection) == [
        "Chapter 2 Page 1.jpg",
        "Chapter 2 Page 9.jpg",
        "Chapter 2 Page 10.jpg",
        "Chapter 10 Page 1.jpg",
        "Image 001.png",
        "Image 01.png",
        "Image 1.png",
        "Image 2.png",
        "Image 10.png",
    ]


def test_natural_sort_handles_long_numbers_unicode_and_extensionless_names() -> None:
    long_low = "x" + "9" * 5000
    long_high = "x1" + "0" * 5000
    assert natural_sort_key(long_low) < natural_sort_key(long_high)
    assert sorted(["Ångström 10", "ångström 2", "Chapter ٢"], key=natural_sort_key) == [
        "Chapter ٢",
        "ångström 2",
        "Ångström 10",
    ]

    collection, _ = _collection(
        [(r"C:\Ålbum 10\image", None), (r"C:\Ålbum 2\image", None)]
    )
    collection.apply_sorting(SortCriterion.PATH)
    assert [entry.source_path for entry in collection.snapshot().entries] == [
        r"C:\Ålbum 2\image",
        r"C:\Ålbum 10\image",
    ]


def test_all_criteria_and_directions_keep_metadata_unknown_last() -> None:
    rows = [
        (r"C:\Folder 10\same.TXT", FileMetadataSnapshot(0, 0, 0)),
        (r"C:\Folder 2\same.txt", FileMetadataSnapshot(10, 20, 30)),
        (r"C:\folder 1\no-size.bin", FileMetadataSnapshot(None, 40, 50)),
        (r"C:\Folder 3\no-metadata.dat", None),
    ]
    collection, entries = _collection(rows)
    for criterion in SortCriterion:
        for direction in SortDirection:
            collection.apply_sorting(criterion, direction)
            current = collection.snapshot().entries
            assert set(entry.entry_id for entry in current) == {
                entry.entry_id for entry in entries.values()
            }
            if criterion is SortCriterion.SIZE:
                assert [entry.original_filename for entry in current[-2:]] == [
                    "no-metadata.dat",
                    "no-size.bin",
                ]
            elif criterion in {SortCriterion.DATE_CREATED, SortCriterion.DATE_MODIFIED}:
                assert [entry.original_filename for entry in current[-1:]] == [
                    "no-metadata.dat"
                ]


def test_name_extension_dates_and_path_use_their_declared_primary_keys() -> None:
    collection, _ = _collection(
        [
            (
                r"C:\Folder 10\zeta.TXT",
                FileMetadataSnapshot(size_bytes=0, created_time_ns=10, modified_time_ns=100),
            ),
            (
                r"C:\Folder 2\alpha.jpg",
                FileMetadataSnapshot(size_bytes=2, created_time_ns=20, modified_time_ns=200),
            ),
            (
                r"C:\Folder 2\Beta.txt",
                FileMetadataSnapshot(size_bytes=1, created_time_ns=None, modified_time_ns=300),
            ),
            (r"C:\Folder 3\unknown.bin", None),
        ]
    )

    collection.apply_sorting(SortCriterion.NAME)
    assert _names(collection) == ["alpha.jpg", "Beta.txt", "unknown.bin", "zeta.TXT"]
    collection.apply_sorting(SortCriterion.EXTENSION)
    assert _names(collection) == ["unknown.bin", "alpha.jpg", "Beta.txt", "zeta.TXT"]
    collection.apply_sorting(SortCriterion.DATE_CREATED)
    assert _names(collection) == ["zeta.TXT", "alpha.jpg", "Beta.txt", "unknown.bin"]
    collection.apply_sorting(SortCriterion.DATE_MODIFIED, SortDirection.DESCENDING)
    assert _names(collection) == ["Beta.txt", "alpha.jpg", "zeta.TXT", "unknown.bin"]
    collection.apply_sorting(SortCriterion.PATH, SortDirection.ASCENDING)
    assert [entry.source_path for entry in collection.snapshot().entries] == [
        r"C:\Folder 2\alpha.jpg",
        r"C:\Folder 2\Beta.txt",
        r"C:\Folder 3\unknown.bin",
        r"C:\Folder 10\zeta.TXT",
    ]


def test_metadata_sort_zero_is_known_and_missing_stays_last_in_both_directions() -> None:
    collection, _ = _collection(
        [
            (r"C:\zero.bin", FileMetadataSnapshot(size_bytes=0)),
            (r"C:\unknown.bin", FileMetadataSnapshot(size_bytes=None)),
            (r"C:\large.bin", FileMetadataSnapshot(size_bytes=9)),
        ]
    )
    collection.apply_sorting(SortCriterion.SIZE, SortDirection.ASCENDING)
    assert _names(collection) == ["zero.bin", "large.bin", "unknown.bin"]
    collection.apply_sorting(direction=SortDirection.DESCENDING)
    assert _names(collection) == ["large.bin", "zero.bin", "unknown.bin"]


def test_natural_ties_use_case_sensitive_name_then_natural_path_deterministically() -> None:
    names = [r"C:\Folder 10\File 01.txt", r"C:\Folder 2\file 1.TXT", r"C:\Folder 3\File 1.txt"]
    orders = []
    for paths in (names, list(reversed(names)), [names[1], names[0], names[2]]):
        collection, _ = _collection([(path, FileMetadataSnapshot(size_bytes=5)) for path in paths])
        collection.apply_sorting(SortCriterion.SIZE)
        orders.append([entry.source_path for entry in collection.snapshot().entries])
    assert orders[0] == orders[1] == orders[2]


def test_explicit_sort_restores_automatic_mode_and_noop_keeps_revision() -> None:
    collection, entries = _collection(
        [(r"C:\z.txt", None), (r"C:\a.txt", None)]
    )
    first = collection.snapshot()
    assert collection.set_order(tuple(entry.entry_id for entry in reversed(first.entries)))
    assert collection.snapshot().ordering_mode.value == "manual"
    assert collection.apply_sorting(SortCriterion.NAME)
    assert collection.snapshot().ordering_mode.value == "automatic"
    assert _names(collection) == ["a.txt", "z.txt"]
    sorted_snapshot = collection.snapshot()
    assert not collection.apply_sorting(SortCriterion.NAME)
    assert collection.is_current(sorted_snapshot)
    assert len(entries) == 2
