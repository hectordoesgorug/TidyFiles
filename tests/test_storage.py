from pathlib import Path

import pytest

from tidyfiles.storage import (
    StorageUnavailableError,
    application_directory,
    resolve_storage_location,
)


def test_selects_writable_portable_location(tmp_path: Path) -> None:
    portable = tmp_path / "portable"
    fallback = tmp_path / "local"

    location = resolve_storage_location(app_dir=portable, local_app_data=fallback)

    assert location.directory == portable.resolve()
    assert location.used_fallback is False
    assert location.config_path.name == "config.json"
    assert location.log_path.name == "tidyfiles.log"


def test_falls_back_when_portable_location_is_unwritable(tmp_path: Path) -> None:
    portable = (tmp_path / "portable").resolve()
    local_app_data = tmp_path / "local"
    probed: list[Path] = []

    def probe(path: Path) -> bool:
        probed.append(path)
        return path != portable

    location = resolve_storage_location(
        app_dir=portable,
        local_app_data=local_app_data,
        writable_probe=probe,
    )

    assert location.directory == (local_app_data / "TidyFiles").resolve()
    assert location.used_fallback is True
    assert probed == [portable, location.directory]


def test_raises_when_fallback_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(StorageUnavailableError, match="fallback storage is writable"):
        resolve_storage_location(
            app_dir=tmp_path / "portable",
            local_app_data=tmp_path / "local",
            writable_probe=lambda _path: False,
        )


def test_frozen_path_uses_executable_directory(tmp_path: Path) -> None:
    executable = tmp_path / "portable" / "TidyFiles.exe"

    assert application_directory(frozen=True, executable=executable) == executable.parent.resolve()


def test_source_path_uses_checkout_root_and_avoids_python_directory(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    package = checkout / "src" / "tidyfiles"
    package.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    other_module = tmp_path / "system-python" / "Lib" / "site-packages" / "tidyfiles.py"

    source_root = application_directory(frozen=False, module_file=package / "storage.py")
    assert source_root == checkout.resolve()
    assert application_directory(frozen=False, module_file=other_module) is None


def test_requires_local_app_data_when_no_source_root_exists(tmp_path: Path) -> None:
    with pytest.raises(StorageUnavailableError, match="LOCALAPPDATA is not set"):
        resolve_storage_location(
            frozen=False,
            module_file=tmp_path / "installed" / "tidyfiles" / "storage.py",
            environ={},
        )
