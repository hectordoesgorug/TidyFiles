from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from test_perceptual_analysis import _collection

import tidyfiles.verification as verification
from tidyfiles.media_tools import MediaCancelled, MediaTool, MediaToolError
from tidyfiles.verification import IntegrityVerifier
from tidyfiles.verification import VerificationStatus as Status


def test_images_and_limited_readability_checks(tmp_path):
    good = tmp_path / "good.png"
    with Image.new("RGB", (13, 17)) as image:
        image.save(good)
    sample = good.read_bytes()
    bad = tmp_path / "bad.png"
    bad.write_bytes(sample[:25])
    unknown = tmp_path / "unknown.jpg"
    unknown.write_bytes(b"unrecognized data")
    empty_image = tmp_path / "empty.webp"
    empty_image.touch()
    empty = tmp_path / "empty.txt"
    empty.touch()
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"x" * 1000)
    entries = _collection(good, bad, unknown, empty_image, empty, audio).snapshot().entries
    checked = IntegrityVerifier().verify(entries, None)
    results = {row.entry.original_filename: row for row in checked}
    assert {name: row.status for name, row in results.items()} == {
        good.name: Status.VERIFIED, bad.name: Status.CORRUPTED, unknown.name: Status.UNSUPPORTED,
        empty_image.name: Status.CORRUPTED, empty.name: Status.VERIFIED,
        audio.name: Status.UNSUPPORTED,
    }
    assert results[bad.name].corruption() is not None
    assert results[unknown.name].corruption() is None
    assert results[empty.name].reason == "Read the initial 0 byte(s)"
    assert "Portable FFprobe unavailable" in results[audio.name].reason


@pytest.mark.parametrize(("extension", "output", "status"), [
    (".wav", MediaToolError("Invalid data found when processing input"), Status.CORRUPTED),
    (".mp3", MediaToolError("Invalid data found when processing input"), Status.INCOMPLETE),
    (".wav", MediaToolError("Generic failure"), Status.INCOMPLETE),
    (".wav", MediaToolError("ffprobe.exe timed out"), Status.INCOMPLETE),
    (".wav", MediaToolError("Permission denied"), Status.INACCESSIBLE),
    (".wav", MediaToolError("Decoder not found"), Status.UNSUPPORTED),
    (".wav", MediaCancelled(), Status.INCOMPLETE),
    (".wav", b'{"streams":[{"codec_type":"audio"}],"format":{"format_name":"wav"}}',
     Status.UNSUPPORTED),
    (".wav", b'{"streams":[{"codec_type":"video","codec_name":"h264"}],'
     b'"format":{"format_name":"mov"}}', Status.UNSUPPORTED),
    (".wav", b'{"streams":[{"codec_type":"audio","codec_name":"mp3"}],'
     b'"format":{"format_name":"mp3"}}', Status.VERIFIED),
])
def test_audio_uses_container_evidence_and_preserves_error_categories(
    tmp_path, monkeypatch, extension, output, status,
):
    path = tmp_path / f"audio{extension}"
    path.write_bytes(b"INVALID WAV HEADER\x00")
    (entry,) = _collection(path).snapshot().entries
    probe = MediaTool("ffprobe", Path("C:/fixture/ffprobe.exe"), "test")
    monkeypatch.setattr(MediaTool, "is_current", lambda self: True)

    def run(_path, args, **options):
        assert args[-1] == str(path) and options["timeout"] == 30
        if isinstance(output, Exception):
            raise output
        return output

    monkeypatch.setattr(verification, "run_media_command", run)
    result = IntegrityVerifier().check(entry, probe)
    assert result.status is status, result
    assert (result.corruption() is not None) == (status is Status.CORRUPTED)


def test_unrecognized_binary_audio_remains_incomplete(tmp_path, monkeypatch):
    path = tmp_path / "unknown.wav"
    path.write_bytes(b"\x01\x02\x03\xfe")
    (entry,) = _collection(path).snapshot().entries
    monkeypatch.setattr(MediaTool, "is_current", lambda self: True)

    def reject(*args, **kwargs):
        raise MediaToolError("Invalid data found when processing input")

    monkeypatch.setattr(verification, "run_media_command", reject)
    result = IntegrityVerifier().check(entry, MediaTool("ffprobe", Path("C:/fixture/ffprobe.exe")))
    assert result.status is Status.INCOMPLETE and result.corruption() is None


def test_heic_uses_registered_decoder(tmp_path):
    from pillow_heif import register_heif_opener

    register_heif_opener()
    name = tmp_path / "photo.heic"
    with Image.new("RGB", (32, 24)) as image:
        image.save(name, format="HEIF")
    entries = _collection(name).snapshot().entries
    (result,) = IntegrityVerifier().verify(entries, None)
    assert result.status is Status.VERIFIED, result


def test_source_change_and_inaccessible_read_have_no_corruption_evidence(tmp_path, monkeypatch):
    name = tmp_path / "image.png"
    with Image.new("RGB", (13, 17)) as image:
        image.save(name)
    (entry,) = _collection(name).snapshot().entries
    verifier = IntegrityVerifier()

    def ordinary_io_error(*_args, **_kwargs):
        raise OSError("device unavailable")

    monkeypatch.setattr(verification.Image, "open", ordinary_io_error)
    result = verifier.check(entry)
    assert (result.status, result.corruption()) == (Status.INACCESSIBLE, None)
    assert "device unavailable" in result.reason
    replacement = tmp_path / "replacement.png"
    replacement.write_bytes(name.read_bytes())
    replacement.replace(name)
    result = verifier.check(entry)
    assert result.status is Status.INACCESSIBLE and "identity changed" in result.reason
    name.unlink()
    assert verifier.check(entry).status is Status.INACCESSIBLE
    assert verifier.check(entry, cancelled=lambda: True).status is Status.INCOMPLETE


@pytest.mark.parametrize(("probe_output", "status"), [
    (b'{"streams":[{"codec_type":"video","codec_name":"mpeg4"}],'
     b'"format":{"format_name":"mov"}}', Status.VERIFIED),
    (b'{"streams":[{"codec_type":"video"}],"format":{"format_name":"mov"}}', Status.UNSUPPORTED),
    (MediaToolError("ffprobe.exe timed out"), Status.INCOMPLETE),
    (MediaToolError("ffprobe.exe exited with code 1: moov atom not found"), Status.CORRUPTED),
    (MediaToolError("ffprobe.exe exited with code 1: permission denied"), Status.INACCESSIBLE),
    (MediaToolError("ffprobe.exe exited with code 1: Invalid data found"), Status.INCOMPLETE),
    (MediaToolError("ffprobe.exe exited with code 1: Decoder not found"), Status.UNSUPPORTED),
    (MediaCancelled(), Status.INCOMPLETE),
])
def test_video_evidence_and_noncorruption_outcomes(tmp_path, monkeypatch, probe_output, status):
    path = tmp_path / "sample.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + bytes(12))
    (entry,) = _collection(path).snapshot().entries
    verifier = IntegrityVerifier()
    assert verifier.check(entry).status is Status.UNSUPPORTED
    bad = tmp_path / "empty.mp4"
    bad.touch()
    (empty,) = _collection(bad).snapshot().entries
    assert verifier.check(empty).status is Status.UNSUPPORTED
    tool = MediaTool("ffprobe", Path("C:/fixture/ffprobe.exe"), "test")
    monkeypatch.setattr(MediaTool, "is_current", lambda self: True)

    def run(_path, args, **options):
        assert args[-1] == str(path)
        assert options["timeout"] == 30
        if isinstance(probe_output, Exception):
            raise probe_output
        return probe_output

    monkeypatch.setattr(verification, "run_media_command", run)
    assert verifier.check(empty, tool).status is Status.CORRUPTED
    result = verifier.check(entry, tool)
    assert result.status is status, result
    assert (result.corruption() is not None) == (status is Status.CORRUPTED)
    assert result.observation is not None
