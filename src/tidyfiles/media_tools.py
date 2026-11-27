"""Portable FFmpeg and FFprobe discovery, import, and bounded execution."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tidyfiles.storage import application_directory

_NAMES = ("ffmpeg", "ffprobe")
_VERSION_TIMEOUT = 4.0


class MediaToolError(OSError):
    """An unavailable tool or a failed media command."""


class MediaCancelled(Exception):
    """The caller cancelled an owned media command."""


def run_media_command(
    executable: Path,
    arguments: tuple[str, ...],
    *,
    timeout: float,
    cancel_check: Callable[[], bool] | None = None,
) -> bytes:
    """Capture one bounded command and reap it on cancellation or timeout."""
    if cancel_check is not None and cancel_check():
        raise MediaCancelled
    if not executable.is_absolute():
        raise MediaToolError("An absolute media executable path is required")
    deadline = time.monotonic() + timeout
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with subprocess.Popen(
        (str(executable), *arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=flags,
    ) as process:
        try:
            while True:
                if cancel_check is not None and cancel_check():
                    raise MediaCancelled
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MediaToolError(f"{executable.name} timed out")
                try:
                    output, errors = process.communicate(timeout=min(remaining, 0.1))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        if cancel_check is not None and cancel_check():
            raise MediaCancelled
        if process.returncode:
            detail = errors.decode("utf-8", errors="replace").strip()
            raise MediaToolError(
                f"{executable.name} exited with code {process.returncode}: {detail[-300:]}"
            )
        return output


@dataclass(frozen=True, slots=True)
class _FileStamp:
    device: int
    inode: int
    size: int
    modified_ns: int


def _stamp(path: Path) -> _FileStamp | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    attributes = getattr(info, "st_file_attributes", 0)
    if not stat.S_ISREG(info.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise MediaToolError(f"{path.name} is not an ordinary executable file")
    # NTFS filename tunneling can change creation time when a staged file takes over a name.
    return _FileStamp(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


@dataclass(frozen=True, slots=True)
class MediaTool:
    name: str
    path: Path | None = None
    version: str | None = None
    problem: str | None = None
    stamp: _FileStamp | None = None

    def is_current(self) -> bool:
        if self.version is None or self.path is None or self.stamp is None:
            return False
        try:
            return _stamp(self.path) == self.stamp
        except OSError:
            return False


@dataclass(frozen=True, slots=True)
class MediaTools:
    ffmpeg: MediaTool
    ffprobe: MediaTool

    @property
    def video_ready(self) -> bool:
        return self.ffmpeg.version is not None and self.ffprobe.version is not None

    def is_current(self) -> bool:
        return self.ffmpeg.is_current() and self.ffprobe.is_current()


def _probe_tool(
    name: str, directory: Path | None, cancel_check: Callable[[], bool] | None
) -> MediaTool:
    if directory is None:
        return MediaTool(name, problem="Application directory is unavailable")
    path = directory / f"{name}.exe"
    try:
        stamp = _stamp(path)
        if stamp is None:
            return MediaTool(name, path, problem="Missing")
        output = run_media_command(
            path, ("-version",), timeout=_VERSION_TIMEOUT, cancel_check=cancel_check
        ).decode("utf-8", errors="replace")
        prefix = f"{name} version "
        first = output.partition("\n")[0]
        if not first.startswith(prefix) or not (version := first[len(prefix):].split()):
            raise MediaToolError(f"Unexpected {name} version output")
        if _stamp(path) != stamp:
            raise MediaToolError(f"{name}.exe changed during validation")
    except OSError as error:
        return MediaTool(name, path, problem=str(error))
    return MediaTool(name, path, version[0], stamp=stamp)


def check_media_tools(
    directory: Path | None = None, *, cancel_check: Callable[[], bool] | None = None
) -> MediaTools:
    """Use the portable root unless checking a user-selected import directory."""
    directory = application_directory() if directory is None else directory.resolve()
    return MediaTools(
        _probe_tool("ffmpeg", directory, cancel_check),
        _probe_tool("ffprobe", directory, cancel_check),
    )


@dataclass(frozen=True, slots=True)
class MediaImportPlan:
    directory: Path
    existing: tuple[_FileStamp | None, ...]

    @property
    def replacements(self) -> tuple[str, ...]:
        return tuple(
            f"{name}.exe" for name, stamp in zip(_NAMES, self.existing, strict=True)
            if stamp is not None
        )


def plan_media_import() -> MediaImportPlan:
    directory = application_directory()
    if directory is None:
        raise MediaToolError("Application directory is unavailable")
    return MediaImportPlan(directory, tuple(_stamp(directory / f"{name}.exe") for name in _NAMES))


def _require_pair(tools: MediaTools) -> None:
    if not tools.video_ready:
        details = (
            f"{tool.name}.exe: {tool.problem}"
            for tool in (tools.ffmpeg, tools.ffprobe) if tool.version is None
        )
        raise MediaToolError(" · ".join(details))


def _rename_to_vacant(source: Path, destination: Path) -> None:
    if _stamp(destination) is not None:
        raise MediaToolError(f"{destination.name} appeared during import")
    # Windows rename fails if the destination appears after the vacancy check.
    source.rename(destination)


def import_media_tools(
    source: Path, plan: MediaImportPlan, *, replace_existing: bool = False
) -> MediaTools:
    """Validate and copy a pair, restoring prior files if installation fails."""
    if plan.replacements and not replace_existing:
        raise MediaToolError("Replacing existing media tools requires approval")
    source = source.resolve()
    selected = check_media_tools(source)
    _require_pair(selected)
    if plan_media_import() != plan:
        raise MediaToolError("The application media tools changed before import")
    if source == plan.directory:
        return selected

    stage = Path(tempfile.mkdtemp(prefix=".tidyfiles-media-", dir=plan.directory))
    backups: list[tuple[Path, Path]] = []
    installed: list[tuple[Path, _FileStamp]] = []
    preserve_stage = False
    try:
        for tool in (selected.ffmpeg, selected.ffprobe):
            assert tool.path is not None
            shutil.copy2(tool.path, stage / tool.path.name)
        if not selected.is_current():
            raise MediaToolError("The selected media tools changed during copying")
        _require_pair(check_media_tools(stage))
        for name, approved in zip(_NAMES, plan.existing, strict=True):
            target = plan.directory / f"{name}.exe"
            new = stage / target.name
            stamp = _stamp(new)
            assert stamp is not None
            if _stamp(target) != approved:
                raise MediaToolError(f"{target.name} changed before installation")
            if approved is not None:
                backup = stage / f"{name}.previous"
                _rename_to_vacant(target, backup)
                backups.append((target, backup))
            _rename_to_vacant(new, target)
            installed.append((target, stamp))
        result = check_media_tools(plan.directory)
        _require_pair(result)
        return result
    except OSError as error:
        rollback_errors = []
        for target, stamp in reversed(installed):
            try:
                if _stamp(target) != stamp:
                    raise MediaToolError(f"{target.name} changed after installation")
                _rename_to_vacant(target, stage / target.name)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        for target, backup in reversed(backups):
            try:
                _rename_to_vacant(backup, target)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        if rollback_errors:
            preserve_stage = True
            raise MediaToolError(
                f"{error}. Import files retained at {stage}. {' · '.join(rollback_errors)}"
            ) from error
        raise
    finally:
        if not preserve_stage:
            shutil.rmtree(stage, ignore_errors=True)
