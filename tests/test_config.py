import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tidyfiles.config import (
    ConfigError,
    default_config,
    get_sorting_preferences,
    load_config,
    save_config,
    set_sorting_preferences,
)
from tidyfiles.sorting import SortCriterion, SortDirection, SortPreferences


def test_missing_file_returns_defaults_with_cleanup_disabled(tmp_path: Path) -> None:
    config = load_config(tmp_path / "config.json")

    assert config == default_config()
    assert config["global_settings"]["auto_clean"] is False
    assert config["global_settings"]["auto_clear_trash_on_exit"] is False
    assert config["global_settings"]["sort_criterion"] == "file_type_name"
    assert config["global_settings"]["sort_direction"] == "ascending"
    assert get_sorting_preferences(config) == SortPreferences()


@pytest.mark.parametrize(
    ("criterion", "direction"),
    [(criterion, direction) for criterion in SortCriterion for direction in SortDirection],
)
def test_sort_preferences_round_trip(
    tmp_path: Path, criterion: SortCriterion, direction: SortDirection
) -> None:
    path = tmp_path / "config.json"
    config = default_config()
    config["global_settings"]["theme"] = "Dark"
    set_sorting_preferences(config, SortPreferences(criterion, direction))

    save_config(path, config)
    loaded = load_config(path)

    assert loaded["global_settings"]["sort_criterion"] == criterion.value
    assert loaded["global_settings"]["sort_direction"] == direction.value
    assert loaded["global_settings"]["theme"] == "Dark"
    assert get_sorting_preferences(loaded) == SortPreferences(criterion, direction)
    assert "ordering_mode" not in loaded["global_settings"]


def test_old_config_without_sort_preferences_gets_defaults_without_rewrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old-config.json"
    content = '{"schema_version": 1, "global_settings": {"theme": "Dark"}, "profiles": {}}\n'
    path.write_text(content, encoding="utf-8")

    loaded = load_config(path)

    assert loaded["global_settings"]["sort_criterion"] == "file_type_name"
    assert loaded["global_settings"]["sort_direction"] == "ascending"
    assert loaded["global_settings"]["theme"] == "Dark"
    assert get_sorting_preferences(loaded) == SortPreferences()
    assert path.read_text(encoding="utf-8") == content


def test_invalid_sort_preferences_fall_back_without_discarding_other_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid-sort.json"
    content = json.dumps(
        {
            "schema_version": 1,
            "global_settings": {
                "sort_criterion": "unrecognized",
                "sort_direction": "sideways",
                "theme": "Dark",
            },
            "profiles": {},
        }
    )
    path.write_text(content, encoding="utf-8")

    loaded = load_config(path)

    assert loaded["global_settings"]["sort_criterion"] == "file_type_name"
    assert loaded["global_settings"]["sort_direction"] == "ascending"
    assert loaded["global_settings"]["theme"] == "Dark"
    assert get_sorting_preferences(loaded) == SortPreferences()
    assert path.read_text(encoding="utf-8") == content


def test_import_does_not_write_configuration(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import tidyfiles.config"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
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
