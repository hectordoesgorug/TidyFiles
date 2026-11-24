import importlib
import json
import os
from pathlib import Path

import pytest

from tidyfiles.config import ConfigError, default_config, load_config, save_config


def test_missing_file_returns_defaults_with_cleanup_disabled(tmp_path: Path) -> None:
    config = load_config(tmp_path / "config.json")

    assert config == default_config()
    assert config["global_settings"]["auto_clean"] is False
    assert config["global_settings"]["auto_clear_trash_on_exit"] is False


def test_import_does_not_write_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tidyfiles.config as config_module

    monkeypatch.chdir(tmp_path)
    importlib.reload(config_module)

    assert list(tmp_path.iterdir()) == []


def test_save_load_round_trip_keeps_settings_and_profiles_separate(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = default_config()
    config["global_settings"]["theme"] = "Dark"
    config["profiles"]["photos"] = {"format": "<Name>"}

    save_config(path, config)

    loaded = load_config(path)
    assert loaded == config
    assert "photos" not in loaded["global_settings"]
    assert "theme" not in loaded["profiles"]


@pytest.mark.parametrize(
    "content, message",
    [
        ("{broken", "Malformed configuration"),
        (
            json.dumps({"schema_version": 2, "global_settings": {}, "profiles": {}}),
            "Unsupported configuration schema version",
        ),
        (
            json.dumps({"schema_version": 1, "global_settings": [], "profiles": {}}),
            "global_settings",
        ),
    ],
)
def test_invalid_configuration_is_reported_without_replacement(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "config.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(path)

    assert path.read_text(encoding="utf-8") == content


def test_atomic_replace_failure_preserves_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.json"
    original = default_config()
    save_config(path, original)
    original_bytes = path.read_bytes()
    updated = default_config()
    updated["global_settings"]["theme"] = "Dark"

    def fail_replace(_source: str, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(ConfigError, match="simulated replace failure"):
        save_config(path, updated)

    assert path.read_bytes() == original_bytes
    assert list(tmp_path.glob(".config.json.*.tmp")) == []


def test_invalid_profiles_are_rejected_on_save(tmp_path: Path) -> None:
    config = default_config()
    config["profiles"] = {"broken": "not an object"}

    with pytest.raises(ConfigError, match="Each named profile"):
        save_config(tmp_path / "config.json", config)

    assert not (tmp_path / "config.json").exists()


def test_save_does_not_replace_damaged_existing_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ConfigError, match="Malformed configuration"):
        save_config(path, default_config())

    assert path.read_text(encoding="utf-8") == "{broken"
