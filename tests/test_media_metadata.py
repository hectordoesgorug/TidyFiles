from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from test_perceptual_analysis import _collection

import tidyfiles.media_metadata as metadata
from tidyfiles.media_metadata import UNAVAILABLE, MediaCells, MediaMetadataProbe
from tidyfiles.media_tools import MediaTool, MediaToolError


def _read(probe, entries, tool=None):
    rows = {}
    probe.read(entries, tool, cancelled=lambda: False,
               publish=lambda entry, cells: rows.__setitem__(entry.original_filename, cells))
    return rows


def test_image_dimensions_cache_and_reject_replacement(tmp_path, monkeypatch):
    path = tmp_path / "image.png"
    with Image.new("RGB", (37, 21)) as image:
        image.save(path)
    entries = _collection(path).snapshot().entries
    probe = MediaMetadataProbe()
    assert _read(probe, entries) == {path.name: MediaCells("37x21")}

    def unexpected(*args, **kwargs):
        pytest.fail("Current image dimensions were not cached")

    monkeypatch.setattr(metadata.Image, "open", unexpected)
    assert _read(probe, entries) == {path.name: MediaCells("37x21")}
    replacement = tmp_path / "replacement.png"
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    assert _read(probe, entries) == {path.name: MediaCells(UNAVAILABLE)}


@pytest.mark.parametrize("failure", [False, True])
def test_video_audio_and_missing_ffprobe(tmp_path, monkeypatch, failure):
    video, audio, other = (tmp_path / name for name in ("video.mp4", "audio.wav", "plain.txt"))
    for path in (video, audio, other):
        path.write_bytes(b"isolated media fixture")
    entries = _collection(video, audio, other).snapshot().entries
    probe = MediaMetadataProbe()
    fallback = {video.name: MediaCells(UNAVAILABLE, UNAVAILABLE),
                audio.name: MediaCells("", UNAVAILABLE)}
    assert _read(probe, entries) == fallback
    calls = []

    def run(executable, arguments, **kwargs):
        stream = arguments[arguments.index("-select_streams") + 1]
        calls.append(stream)
        if failure:
            raise MediaToolError("probe timeout")
        if stream == "V:0":
            return b'{"streams":[{"width":1280,"height":720}],"format":{"duration":"65.7"}}'
        return b'{"streams":[{"duration":"4.4"}]}'

    monkeypatch.setattr(MediaTool, "is_current", lambda self: True)
    monkeypatch.setattr(metadata, "run_media_command", run)
    tool = MediaTool("ffprobe", Path("C:/fixture/ffprobe.exe"), "fixture")
    expected = fallback if failure else {
        video.name: MediaCells("1280x720", "1:05"), audio.name: MediaCells("", "0:04"),
    }
    assert _read(probe, entries, tool) == expected
    assert set(calls) == {"V:0", "a:0"}
