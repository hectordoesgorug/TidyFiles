from __future__ import annotations

import io
import json
import random
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from PIL import Image, ImageDraw
from pillow_heif import register_heif_opener
from test_discovery import _complete

import tidyfiles.perceptual_analysis as perceptual
from tidyfiles.collection import FileSystemIdentitySnapshot, ImportMode, WorkingCollection
from tidyfiles.config import DuplicatePreferences
from tidyfiles.discovery import FilesystemDiscovery
from tidyfiles.media_tools import MediaCancelled, MediaTool, MediaToolError, MediaTools
from tidyfiles.perceptual_analysis import (
    SAMPLE_PERCENTAGES,
    FrameSample,
    MediaKind,
    PerceptualAnalyzer,
    PerceptualFile,
    PerceptualState,
    perceptual_similarity,
    probable_groups,
)

_SETTINGS = DuplicatePreferences(perceptual_enabled=True)


def _collection(*paths: Path) -> WorkingCollection:
    collection = WorkingCollection()
    _complete(FilesystemDiscovery(collection).begin_files(paths))
    return collection


def _pattern() -> Image.Image:
    image = Image.new("RGB", (96, 96), "#203090")
    draw = ImageDraw.Draw(image)
    draw.ellipse((7, 11, 53, 70), fill="#80d0f0")
    draw.polygon([(31, 4), (70, 70), (78, 48)], fill="#f5b020")
    return image


def _encoded_frame() -> bytes:
    with _pattern() as image, io.BytesIO() as stream:
        image.save(stream, format="PNG")
        return stream.getvalue()


def _available_tools(monkeypatch) -> MediaTools:
    monkeypatch.setattr(MediaTools, "is_current", lambda self: True)
    return MediaTools(
        MediaTool("ffmpeg", Path("C:/fixture/ffmpeg.exe"), "fixture"),
        MediaTool("ffprobe", Path("C:/fixture/ffprobe.exe"), "fixture"),
    )


def test_image_hashing_is_independent_of_media_tools_and_preserves_sources(tmp_path):
    png, copied, jpeg, different, unreadable, outside, video = (
        tmp_path / name for name in
        ("a.png", "b.png", "c.jpg", "d.png", "broken.raw", "outside.txt", "clip.mp4")
    )
    with _pattern() as image:
        image.save(png)
        image.save(jpeg, quality=91)
    copied.write_bytes(png.read_bytes())
    rng = random.Random(12)
    with Image.frombytes("L", (96, 96), rng.randbytes(96 * 96)) as noise:
        noise.save(different)
    for file in (unreadable, outside, video):
        file.write_bytes(b"unsupported fixture")
    paths = (png, copied, jpeg, different, unreadable, outside, video)
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    collection = _collection(*paths)
    snapshot = collection.snapshot()
    result = PerceptualAnalyzer().analyze(snapshot, _SETTINGS)

    assert result.state is PerceptualState.COMPLETE
    by_name = {file.entry.original_filename: file for file in result.files}
    a, b, c, d = (by_name[path.name] for path in (png, copied, jpeg, different))
    assert a.image_hash is not None and a.image_hash.bit_length() <= 64
    assert perceptual_similarity(a, b) == 100
    assert (perceptual_similarity(a, c) or 0) >= 80
    dissimilarity = perceptual_similarity(a, d)
    assert dissimilarity is not None and dissimilarity < 80
    assert by_name[unreadable.name].problem
    assert "requires local FFmpeg" in (by_name[video.name].problem or "")
    assert outside.name not in by_name
    (group,) = result.groups
    assert group.reference_id == a.entry.entry_id
    assert {match.file.entry.entry_id for match in group.members} == {
        file.entry.entry_id for file in (a, b, c)
    }
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before
    assert collection.snapshot() == snapshot


@pytest.mark.parametrize("mode", ["L", "RGB", "RGBA", "P"])
def test_image_color_modes_produce_64_bit_phash(tmp_path, mode):
    path = tmp_path / "mode.png"
    with _pattern() as source, source.convert(mode) as image:
        image.save(path)
    (file,) = PerceptualAnalyzer().analyze(_collection(path).snapshot(), _SETTINGS).files
    assert file.image_hash is not None and file.image_hash.bit_length() <= 64
    assert file.problem is None


def test_available_heif_integration_reads_heic(tmp_path):
    register_heif_opener()
    path = tmp_path / "sample.heic"
    with _pattern() as image:
        image.save(path, format="HEIF")
    before = path.read_bytes()
    (file,) = PerceptualAnalyzer().analyze(_collection(path).snapshot(), _SETTINGS).files
    assert file.image_hash is not None and file.problem is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("preferences", [DuplicatePreferences(), replace(_SETTINGS, enabled=False)])
def test_disabled_preferences_do_not_start_analysis(preferences, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Disabled perceptual hashing started")

    monkeypatch.setattr(perceptual, "_observe", unexpected)
    result = PerceptualAnalyzer().analyze(WorkingCollection().snapshot(), preferences)
    assert result.state is PerceptualState.DISABLED and not result.files and not result.groups


def test_exact_exclusions_are_never_perceptual_candidates(tmp_path):
    keeper, duplicate = tmp_path / "a.png", tmp_path / "b.png"
    keeper.write_bytes(_encoded_frame())
    duplicate.write_bytes(keeper.read_bytes())
    collection = WorkingCollection()
    session_id = collection.begin_import(ImportMode.ADD_FILES)
    ids = []
    for path in (keeper, duplicate):
        info = path.stat()
        identity = FileSystemIdentitySnapshot(info.st_dev, info.st_ino)
        entry = collection.register_path(
            session_id, path, filesystem_identity=identity
        ).entry
        ids.append(entry.entry_id)
    prepared = collection.prepare_import_order(session_id)
    collection.finalize_import(
        session_id, prepared_order=replace(prepared, duplicate_pairs=((ids[1], ids[0]),))
    )
    result = PerceptualAnalyzer().analyze(collection.snapshot(), _SETTINGS)
    assert [file.entry.entry_id for file in result.files] == [ids[0]]
    assert not result.groups and keeper.read_bytes() == duplicate.read_bytes()


@pytest.mark.parametrize("duration", [0.5, 6.0, 12.0])
def test_video_samples_use_stream_duration_and_keep_positions(tmp_path, monkeypatch, duration):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"simulated video")
    tools = _available_tools(monkeypatch)
    frame = _encoded_frame()
    commands = []

    def run(executable, args, **kwargs):
        assert 0 < kwargs["timeout"] <= 15
        if executable == tools.ffprobe.path:
            return json.dumps({
                "streams": [{"duration": str(duration)}], "format": {"duration": "100"}
            }).encode()
        commands.append(args)
        return frame

    monkeypatch.setattr(perceptual, "run_media_command", run)
    (file,) = PerceptualAnalyzer().analyze(
        _collection(path).snapshot(), _SETTINGS, tools=tools
    ).files

    assert not file.problem
    assert tuple(sample.percent for sample in file.frames) == SAMPLE_PERCENTAGES
    times = [duration * value / 100 for value in SAMPLE_PERCENTAGES]
    assert [sample.seconds for sample in file.frames] == pytest.approx(times)
    assert [float(args[args.index("-ss") + 1]) for args in commands] == pytest.approx(times)
    assert len({sample.hash_value for sample in file.frames}) == 1
    assert all(args[-1] == "pipe:1" for args in commands)


@pytest.mark.parametrize("failure", ["empty_frame", "timeout"])
def test_failed_video_frame_cannot_shift_correspondence(tmp_path, monkeypatch, failure):
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    for path in (first, second):
        path.write_bytes(b"simulated video")
    tools = _available_tools(monkeypatch)
    frame = _encoded_frame()

    def run(executable, args, **kwargs):
        if executable == tools.ffprobe.path:
            return b'{"streams":[{}],"format":{"duration":"2"}}'
        if str(first) in args and args[args.index("-ss") + 1] == "0.600000000":
            if failure == "timeout":
                raise MediaToolError("ffmpeg.exe timed out")
            return b""
        return frame

    monkeypatch.setattr(perceptual, "run_media_command", run)
    snapshot = _collection(first, second).snapshot()
    result = PerceptualAnalyzer().analyze(snapshot, _SETTINGS, tools=tools)
    file, match = result.files
    assert file.problem == "Incomplete video frame extraction"
    assert [sample.percent for sample in file.frames] == list(SAMPLE_PERCENTAGES)
    assert file.frames[1].hash_value is None and file.frames[1].problem
    assert all(sample.hash_value is not None for sample in file.frames[2:])
    assert perceptual_similarity(file, match) is None and not result.groups


@pytest.mark.parametrize("output", [
    b'{"streams":[{"duration":"N/A"}]}',
    b'{"streams":[{"duration":"nan"}],"format":{"duration":"-1"}}',
    b'{"streams":[],"format":{"duration":"4"}}',
    b'not json',
])
def test_invalid_video_duration_is_reported_before_frame_reads(tmp_path, monkeypatch, output):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video fixture")
    tools = _available_tools(monkeypatch)

    def run(executable, *_args, **_kwargs):
        assert executable == tools.ffprobe.path
        return output

    monkeypatch.setattr(perceptual, "run_media_command", run)
    (file,) = PerceptualAnalyzer().analyze(
        _collection(path).snapshot(), _SETTINGS, tools=tools
    ).files
    assert file.problem and not file.frames


@pytest.mark.parametrize("threshold,differences", [(70, 19), (80, 12), (90, 6), (100, 0)])
def test_thresholds_and_hamming_distance_are_inclusive(tmp_path, threshold, differences):
    files = []
    for name in ("a.png", "b.png", "c.png"):
        path = tmp_path / name
        path.write_bytes(b"test")
        files.append(path)
    a, b, c = _collection(*files).snapshot().entries
    reference = PerceptualFile(a, MediaKind.IMAGE, 10, image_hash=0)
    passing = PerceptualFile(b, MediaKind.IMAGE, 20, image_hash=(1 << differences) - 1)
    failing = PerceptualFile(c, MediaKind.IMAGE, 30, image_hash=(1 << (differences + 1)) - 1)
    assert perceptual_similarity(reference, passing) == (64 - differences) / 64 * 100
    assert perceptual_similarity(reference, failing) == (63 - differences) / 64 * 100
    (group,) = probable_groups((reference, passing, failing), threshold)
    assert group.reference_id == a.entry_id
    assert tuple(match.file for match in group.members) == (passing, reference)
    assert probable_groups((reference, passing), 100) == (() if differences else (group,))

    def video(entry, bits):
        frames = tuple(
            FrameSample(percent, 0, (bits >> (index * 64)) & ((1 << 64) - 1))
            for index, percent in enumerate(SAMPLE_PERCENTAGES)
        )
        return PerceptualFile(entry, MediaKind.VIDEO, frames=frames)

    boundary = (1 << ((100 - threshold) * 320 // 100)) - 1
    video_reference, equal, below = video(a, 0), video(b, boundary), video(c, (boundary << 1) | 1)
    assert perceptual_similarity(video_reference, equal) == threshold
    (video_group,) = probable_groups((video_reference, equal, below), threshold)
    assert tuple(match.file for match in video_group.members) == (video_reference, equal)


def test_video_comparison_averages_only_corresponding_hashes_and_never_crosses_types(tmp_path):
    path = tmp_path / "fixture.png"
    path.write_bytes(b"sample")
    (entry,) = _collection(path).snapshot().entries
    same, different = 0, (1 << 64) - 1
    reference = PerceptualFile(entry, MediaKind.VIDEO, frames=tuple(
        FrameSample(percent, 0, same if index == 0 else different)
        for index, percent in enumerate(SAMPLE_PERCENTAGES)
    ))
    candidate = replace(reference, frames=tuple(
        FrameSample(percent, 0, same) for percent in SAMPLE_PERCENTAGES
    ))
    assert perceptual_similarity(reference, candidate) == 20
    misaligned = replace(candidate, frames=candidate.frames[::-1])
    image = PerceptualFile(entry, MediaKind.IMAGE, image_hash=0)
    assert perceptual_similarity(reference, misaligned) is None
    assert perceptual_similarity(reference, image) is None


def test_grouping_is_reference_based_not_transitive(tmp_path):
    paths = [tmp_path / f"{index}.png" for index in range(3)]
    for path in paths:
        path.write_bytes(b"sample")
    entries = _collection(*paths).snapshot().entries
    files = tuple(PerceptualFile(entry, MediaKind.IMAGE, 20, image_hash=hashed)
                  for entry, hashed in zip(entries, (0, 255, 65535), strict=True))
    assert perceptual_similarity(files[0], files[1]) == 87.5
    assert perceptual_similarity(files[1], files[2]) == 87.5
    assert perceptual_similarity(files[0], files[2]) == 75
    (group,) = probable_groups(files, 80)
    assert tuple(member.file for member in group.members) == files[:2]


def test_worker_completion_order_caching_and_replacement(tmp_path, monkeypatch):
    first, second = tmp_path / "a.png", tmp_path / "b.png"
    for path in (first, second):
        path.write_bytes(_encoded_frame())
    collection = _collection(first, second)
    snapshot = collection.snapshot()
    serial = PerceptualAnalyzer().analyze(snapshot, replace(_SETTINGS, workers=1))
    analyzer = PerceptualAnalyzer()
    original = perceptual._hash_file
    second_done = Event()
    order = []

    def reversed_completion(observer, source, kind, tools, cancel_check):
        if source.entry.source_path == str(first):
            assert second_done.wait(5)
        result = original(observer, source, kind, tools, cancel_check)
        order.append(source.entry.source_path)
        if source.entry.source_path == str(second):
            second_done.set()
        return result

    monkeypatch.setattr(perceptual, "_hash_file", reversed_completion)
    parallel = analyzer.analyze(snapshot, replace(_SETTINGS, workers=2))
    assert order == [str(second), str(first)]
    assert parallel == serial
    order.clear()
    assert analyzer.analyze(snapshot, _SETTINGS) == serial and not order
    replacement = tmp_path / "changed"
    replacement.write_bytes(second.read_bytes())
    replacement.replace(second)
    result = analyzer.analyze(snapshot, _SETTINGS)
    assert result.files[1].problem and not result.groups and not order


def test_cancelled_video_work_publishes_no_partial_result(tmp_path, monkeypatch):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"fake video")
    tools = _available_tools(monkeypatch)
    cancelled = Event()

    def run(executable, args, *, cancel_check, **kwargs):
        if executable == tools.ffprobe.path:
            return b'{"streams":[{"duration":"2"}]}'
        cancelled.set()
        assert cancel_check is not None and cancel_check()
        raise MediaCancelled

    monkeypatch.setattr(perceptual, "run_media_command", run)
    result = PerceptualAnalyzer().analyze(
        _collection(path).snapshot(), _SETTINGS, tools=tools, cancel_check=cancelled.is_set
    )
    assert result.state is PerceptualState.CANCELLED and not result.files and not result.groups
