from __future__ import annotations

import errno
import subprocess
import sys
from pathlib import Path

import pytest

import tidyfiles.media_tools as media
from tidyfiles.media_tools import MediaCancelled, MediaToolError


@pytest.fixture
def tools(tmp_path, monkeypatch):
    root = tmp_path / "portable"
    source = tmp_path / "selected"
    root.mkdir()
    source.mkdir()
    monkeypatch.setattr(media, "application_directory", lambda: root)
    calls = []

    def version(path, arguments, *, timeout, cancel_check=None):
        calls.append((path, arguments, timeout))
        contents = path.read_bytes()
        if contents == b"timeout":
            raise MediaToolError(f"{path.name} timed out")
        if contents == b"exit":
            raise MediaToolError(f"{path.name} exited with code 1")
        if contents == b"wrong":
            return b"arbitrary executable output"
        return f"{path.stem} version {contents.decode()} Copyright test\n".encode()

    monkeypatch.setattr(media, "run_media_command", version)
    return root, source, calls


def _pair(directory, version=b"9.1"):
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (directory / name).write_bytes(version)


def test_only_portable_root_is_used_with_working_directory_and_path_elsewhere(
    tools, monkeypatch
):
    root, source, calls = tools
    _pair(source)
    monkeypatch.chdir(source)
    monkeypatch.setenv("PATH", str(source))

    unavailable = media.check_media_tools()
    assert not unavailable.video_ready
    assert unavailable.ffmpeg.problem == unavailable.ffprobe.problem == "Missing"
    assert not calls

    _pair(root, b"7.0")
    ready = media.check_media_tools()
    assert ready.video_ready and ready.is_current()
    assert ready.ffmpeg.version == ready.ffprobe.version == "7.0"
    assert [path.parent for path, _, _ in calls] == [root, root]
    assert all(args == ("-version",) and 0 < timeout <= 5 for _, args, timeout in calls)


@pytest.mark.parametrize("problem", [b"timeout", b"exit", b"wrong", None])
def test_individual_dependency_failures_keep_valid_version(tools, problem):
    root, _, _ = tools
    (root / "ffmpeg.exe").write_bytes(b"9.1")
    if problem is not None:
        (root / "ffprobe.exe").write_bytes(problem)
    status = media.check_media_tools()
    assert status.ffmpeg.version == "9.1"
    assert status.ffprobe.version is None and status.ffprobe.problem
    assert not status.video_ready


def test_import_copies_only_valid_pair_and_rechecks_installed_versions(tools):
    root, source, calls = tools
    _pair(source)
    (source / "ffplay.exe").write_bytes(b"unused")

    status = media.import_media_tools(source, media.plan_media_import())

    assert status.video_ready and status.is_current()
    assert sorted(path.name for path in root.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert sorted(path.name for path in source.iterdir()) == [
        "ffmpeg.exe", "ffplay.exe", "ffprobe.exe",
    ]
    assert all(path.read_bytes() == b"9.1" for path in root.iterdir())
    assert [path.parent for path, _, _ in calls][-2:] == [root, root]


def test_replacement_needs_approval_for_the_observed_files(tools):
    root, source, _ = tools
    _pair(root, b"8")
    _pair(source)
    plan = media.plan_media_import()
    assert plan.replacements == ("ffmpeg.exe", "ffprobe.exe")
    with pytest.raises(MediaToolError, match="requires approval"):
        media.import_media_tools(source, plan)
    (root / "ffmpeg.exe").write_bytes(b"external replacement")
    with pytest.raises(MediaToolError, match="changed before import"):
        media.import_media_tools(source, plan, replace_existing=True)
    assert (root / "ffmpeg.exe").read_bytes() == b"external replacement"
    assert (root / "ffprobe.exe").read_bytes() == b"8"

    status = media.import_media_tools(source, media.plan_media_import(), replace_existing=True)
    assert status.is_current() and status.ffmpeg.version == status.ffprobe.version == "9.1"
    assert all(path.read_bytes() == b"9.1" for path in source.iterdir())


@pytest.mark.parametrize("failure", ["invalid", "no_space", "second_rename", "final_check"])
def test_failed_replacement_preserves_working_pair(tools, monkeypatch, failure):
    root, source, _ = tools
    _pair(root, b"8")
    _pair(source)
    if failure == "invalid":
        (source / "ffprobe.exe").write_bytes(b"wrong")
    elif failure == "no_space":
        def no_space(*args):
            raise OSError(errno.ENOSPC, "fixture disk full")
        monkeypatch.setattr(media.shutil, "copy2", no_space)
    elif failure == "second_rename":
        rename = media._rename_to_vacant

        def interrupted(new, target):
            if target == root / "ffprobe.exe" and new.name == "ffprobe.exe":
                raise PermissionError("fixture replacement blocked")
            rename(new, target)

        monkeypatch.setattr(media, "_rename_to_vacant", interrupted)
    else:
        check = media.check_media_tools

        def invalid_final(directory=None, **kwargs):
            if directory == root:
                return media.MediaTools(media.MediaTool("ffmpeg"), media.MediaTool("ffprobe"))
            return check(directory, **kwargs)

        monkeypatch.setattr(media, "check_media_tools", invalid_final)
    with pytest.raises(OSError):
        media.import_media_tools(source, media.plan_media_import(), replace_existing=True)
    assert sorted(path.name for path in root.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]
    assert all(path.read_bytes() == b"8" for path in root.iterdir())
    assert (source / "ffmpeg.exe").read_bytes() == b"9.1"


@pytest.mark.parametrize("cancel", [False, True])
def test_bounded_command_stops_and_reaps_its_own_process(tmp_path, monkeypatch, cancel):
    flag = tmp_path / "started"
    children = []
    popen = subprocess.Popen

    def track(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(media.subprocess, "Popen", track)
    code = "from pathlib import Path; import sys, time; Path(sys.argv[1]).touch(); time.sleep(30)"
    with pytest.raises(MediaCancelled if cancel else MediaToolError):
        media.run_media_command(
            Path(sys.executable), ("-c", code, str(flag)),
            timeout=5 if cancel else 0.3,
            cancel_check=flag.is_file if cancel else None,
        )
    assert len(children) == 1 and children[0].poll() is not None
    if cancel:
        assert flag.exists()
