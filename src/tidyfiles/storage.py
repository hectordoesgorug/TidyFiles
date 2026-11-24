"""Portable locations for ordinary application configuration and logs."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


class StorageUnavailableError(OSError):
    """Raised when neither portable nor fallback storage can be used."""


@dataclass(frozen=True)
class StorageLocation:
    directory: Path
    used_fallback: bool

    @property
    def config_path(self) -> Path:
        return self.directory / "config.json"

    @property
    def log_path(self) -> Path:
        return self.directory / "tidyfiles.log"


def application_directory(
    *,
    frozen: bool | None = None,
    executable: Path | str | None = None,
    module_file: Path | str | None = None,
) -> Path | None:
    """Find the frozen executable directory or the source checkout root."""
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))

    if frozen:
        executable_path = Path(executable or sys.executable).resolve()
        return executable_path.parent

    source_path = Path(module_file or __file__).resolve()
    for parent in source_path.parents:
        if (parent / "pyproject.toml").is_file() and (
            parent / "src" / "tidyfiles"
        ).is_dir():
            return parent
    return None


def _probe_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".tidyfiles-write-test-", dir=directory
        ) as probe:
            probe.write(b"write check")
            probe.flush()
            os.fsync(probe.fileno())
        return True
    except OSError:
        return False


def resolve_storage_location(
    *,
    app_dir: Path | str | None = None,
    local_app_data: Path | str | None = None,
    frozen: bool | None = None,
    executable: Path | str | None = None,
    module_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    writable_probe: Callable[[Path], bool] | None = None,
) -> StorageLocation:
    """Choose writable portable storage, falling back to Local AppData/TidyFiles."""
    probe = writable_probe or _probe_writable
    preferred = Path(app_dir) if app_dir is not None else application_directory(
        frozen=frozen, executable=executable, module_file=module_file
    )
    if preferred is not None:
        preferred = preferred.resolve()
        if probe(preferred):
            return StorageLocation(preferred, used_fallback=False)

    if local_app_data is None:
        environment = os.environ if environ is None else environ
        local_app_data = environment.get("LOCALAPPDATA")
    if not local_app_data:
        raise StorageUnavailableError(
            "Portable storage is unavailable and LOCALAPPDATA is not set."
        )

    fallback = (Path(local_app_data) / "TidyFiles").resolve()
    if probe(fallback):
        return StorageLocation(fallback, used_fallback=True)
    raise StorageUnavailableError(
        f"Neither portable storage nor fallback storage is writable: {fallback}"
    )
