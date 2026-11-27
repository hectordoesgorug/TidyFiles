"""Opt-in integration tests for owner-supplied portable executables."""

from __future__ import annotations

import hashlib
import shutil
import wave
from pathlib import Path

import pytest
from test_perceptual_analysis import _SETTINGS, _collection

import tidyfiles.media_tools as media
from tidyfiles.media_metadata import MediaCells, MediaMetadataProbe
from tidyfiles.media_tools import MediaCancelled, MediaToolError
from tidyfiles.perceptual_analysis import (
    SAMPLE_PERCENTAGES,
    PerceptualAnalyzer,
    perceptual_similarity,
)
from tidyfiles.storage import application_directory
from tidyfiles.verification import IntegrityVerifier, VerificationStatus


@pytest.fixture
def local_tools():
    root = application_directory()
    if root is None or not all((root / name).is_file() for name in ("ffmpeg.exe", "ffprobe.exe")):
        pytest.skip("Supply the portable FFmpeg and FFprobe pair to run real-media integration")
    tools = media.check_media_tools()
    assert tools.video_ready and tools.is_current(), tools
    return root, tools


def _create_video(ffmpeg: Path, output: Path, *inputs: str) -> None:
    arguments = (
        "-v", "error", "-nostdin",
        *(argument for source in inputs for argument in ("-f", "lavfi", "-i", source)),
        *(("-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0") if len(inputs) == 2 else ()),
        "-c:v", "mpeg4", "-threads", "1", str(output),
    )
    media.run_media_command(ffmpeg, arguments, timeout=15)


def test_real_video_verification_and_missing_probe(local_tools, tmp_path):
    _, tools = local_tools
    assert tools.ffmpeg.path is not None
    good, truncated = tmp_path / "good.mp4", tmp_path / "truncated.mp4"
    _create_video(tools.ffmpeg.path, good, "testsrc2=s=64x48:r=5:d=1")
    contents = good.read_bytes()
    assert contents[4:8] == b"ftyp"
    truncated.write_bytes(contents[:int.from_bytes(contents[:4], "big")])
    entries = _collection(good, truncated).snapshot().entries
    verifier = IntegrityVerifier()
    verified, corrupt = verifier.verify(entries, tools.ffprobe)
    assert verified.status is VerificationStatus.VERIFIED, verified
    assert corrupt.status is VerificationStatus.CORRUPTED, corrupt
    assert "moov" in corrupt.reason
    assert all(
        result.status is VerificationStatus.UNSUPPORTED for result in verifier.verify(entries, None)
    )
    assert good.read_bytes() == contents and truncated.stat().st_size < len(contents)


def test_real_audio_container_verification(local_tools, tmp_path):
    _, tools = local_tools
    assert tools.ffmpeg.path is not None
    wav, empty, bad, truncated, mp3, flac, aac, mislabeled = (
        tmp_path / name for name in (
            "tone.wav", "empty.wav", "bad.wav", "truncated.wav", "tone.mp3", "tone.flac",
            "tone.aac", "mislabeled.wav",
        )
    )
    for path, frames in ((wav, bytes(1600)), (empty, b"")):
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            stream.writeframes(frames)
    bad.write_bytes(b"INVALID WAV HEADER\x00")
    truncated.write_bytes(wav.read_bytes()[:-16])
    for path in (mp3, flac, aac):
        media.run_media_command(
            tools.ffmpeg.path,
            ("-v", "error", "-i", str(wav), "-ar", "44100", str(path)),
            timeout=15,
        )
    shutil.copyfile(mp3, mislabeled)
    paths = (wav, empty, bad, truncated, mp3, flac, aac, mislabeled)
    contents = {path: path.read_bytes() for path in paths}
    entries = _collection(*paths).snapshot().entries
    checked = IntegrityVerifier().verify(entries, tools.ffprobe)
    results = {row.entry.original_filename: row for row in checked}
    for path in paths:
        expected = (
            VerificationStatus.CORRUPTED if path in {bad, truncated}
            else VerificationStatus.VERIFIED
        )
        assert results[path.name].status is expected, results[path.name]
        assert path.read_bytes() == contents[path]
    assert "signature" in results[bad.name].reason
    assert "size" in results[truncated.name].reason


@pytest.mark.parametrize("duration,rate", [(0.5, 2), (0.6, 5), (6, 10), (12, 10)])
def test_real_version_duration_and_frames_at_all_positions(
    local_tools, tmp_path, monkeypatch, duration, rate
):
    root, tools = local_tools
    monkeypatch.chdir(tmp_path)
    assert media.check_media_tools() == tools
    assert tools.ffmpeg.path is not None and tools.ffmpeg.path.parent == root
    print(f"Real versions: FFmpeg {tools.ffmpeg.version}, FFprobe {tools.ffprobe.version}")
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _create_video(tools.ffmpeg.path, first, f"testsrc2=s=64x48:r={rate}:d={duration}")
    shutil.copyfile(first, second)
    original = {path: hashlib.sha256(path.read_bytes()).digest() for path in (first, second)}
    snapshot = _collection(first, second).snapshot()
    result = PerceptualAnalyzer().analyze(snapshot, _SETTINGS, tools=tools)
    a, b = result.files
    assert all(file.problem is None for file in result.files), result.files
    assert tuple(sample.percent for sample in a.frames) == SAMPLE_PERCENTAGES
    assert [sample.seconds for sample in a.frames] == pytest.approx(
        [duration * percent / 100 for percent in SAMPLE_PERCENTAGES]
    )
    assert perceptual_similarity(a, b) == 100 and len(result.groups) == 1
    assert {path: hashlib.sha256(path.read_bytes()).digest() for path in original} == original
    assert set(tmp_path.iterdir()) == set(original)


def test_real_videos_with_same_intro_diverge_in_later_samples(local_tools, tmp_path):
    _, tools = local_tools
    ffmpeg = tools.ffmpeg.path
    assert ffmpeg is not None
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    intro = "testsrc2=s=96x64:r=10:d=2"
    _create_video(ffmpeg, first, intro, "testsrc2=s=96x64:r=10:d=10")
    _create_video(ffmpeg, second, intro, "smptebars=s=96x64:r=10:d=10")

    snapshot = _collection(first, second).snapshot()
    result = PerceptualAnalyzer().analyze(snapshot, _SETTINGS, tools=tools)
    a, b = result.files
    assert not a.problem and not b.problem
    assert a.frames[0].hash_value == b.frames[0].hash_value
    assert all(x.hash_value != y.hash_value for x, y in zip(a.frames[1:], b.frames[1:]))
    similarity = perceptual_similarity(a, b)
    assert similarity is not None and similarity < 80 and not result.groups


@pytest.mark.parametrize("cancel", [False, True])
def test_real_ffmpeg_timeout_and_cancellation_reap_child(local_tools, monkeypatch, cancel):
    _, tools = local_tools
    ffmpeg = tools.ffmpeg.path
    assert ffmpeg is not None
    children = []
    checks = 0
    popen = media.subprocess.Popen

    def track(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def should_cancel():
        nonlocal checks
        checks += 1
        return checks == 3

    monkeypatch.setattr(media.subprocess, "Popen", track)
    with pytest.raises(MediaCancelled if cancel else MediaToolError):
        media.run_media_command(
            ffmpeg, ("-v", "error", "-re", "-f", "lavfi", "-i", "testsrc2=s=32x32",
                     "-t", "30", "-f", "null", "-"),
            timeout=5 if cancel else 0.2,
            cancel_check=should_cancel if cancel else None,
        )
    assert len(children) == 1 and children[0].poll() is not None


def test_real_import_into_disposable_portable_root_preserves_supplied_executables(
    local_tools, tmp_path, monkeypatch
):
    original_root, original = local_tools
    target = tmp_path / "app"
    target.mkdir()
    monkeypatch.setattr(media, "application_directory", lambda: target)
    imported = media.import_media_tools(original_root, media.plan_media_import())
    assert imported.video_ready and imported.is_current()
    assert imported.ffmpeg.version == original.ffmpeg.version
    assert imported.ffprobe.version == original.ffprobe.version
    assert original.is_current()
    assert sorted(path.name for path in target.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]


def test_real_video_resolution_and_audio_duration(local_tools, tmp_path):
    _, tools = local_tools
    ffmpeg = tools.ffmpeg.path
    assert ffmpeg is not None
    video, audio = tmp_path / "video.mp4", tmp_path / "audio.wav"
    _create_video(ffmpeg, video, "testsrc2=s=96x64:r=10:d=2")
    media.run_media_command(
        ffmpeg, ("-v", "error", "-f", "lavfi", "-i", "sine=duration=3", str(audio)), timeout=10,
    )
    rows = {}
    MediaMetadataProbe().read(
        _collection(video, audio).snapshot().entries, tools.ffprobe,
        cancelled=lambda: False,
        publish=lambda entry, cells: rows.__setitem__(entry.original_filename, cells),
    )
    assert rows == {video.name: MediaCells("96x64", "0:02"), audio.name: MediaCells("", "0:03")}
