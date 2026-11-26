"""Versioned configuration loading and atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, MutableMapping

from tidyfiles.sorting import SortCriterion, SortDirection, SortPreferences

SCHEMA_VERSION = 1
_DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "global_settings": {
        "auto_clean": False,
        "auto_clear_trash_on_exit": False,
        "sort_criterion": SortCriterion.FILE_TYPE_AND_NAME.value,
        "sort_direction": SortDirection.ASCENDING.value,
    },
    "profiles": {},
}


class ConfigError(ValueError):
    """Raised when configuration is invalid, unreadable, or cannot be saved."""


def default_config() -> dict[str, Any]:
    """Return a fresh default configuration."""
    return deepcopy(_DEFAULT_CONFIG)


def _validate_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigError("Configuration must be a JSON object.")
    version = config.get("schema_version")
    if type(version) is not int:
        raise ConfigError("Configuration schema_version must be an integer.")
    if version != SCHEMA_VERSION:
        raise ConfigError(f"Unsupported configuration schema version: {version}.")
    if not isinstance(config.get("global_settings"), dict):
        raise ConfigError("Configuration global_settings must be an object.")
    if any(
        key in config["global_settings"] and type(config["global_settings"][key]) is not bool
        for key in ("auto_clean", "auto_clear_trash_on_exit")
    ):
        raise ConfigError("Cleanup settings must be booleans.")
    profiles = config.get("profiles")
    if not isinstance(profiles, dict):
        raise ConfigError("Configuration profiles must be an object.")
    if any(
        not isinstance(name, str) or not isinstance(profile, dict)
        for name, profile in profiles.items()
    ):
        raise ConfigError("Each named profile must be an object with a string name.")
    return config


def _normalize_sorting_settings(settings: MutableMapping[str, Any]) -> None:
    try:
        criterion = SortCriterion(settings.get("sort_criterion"))
    except (TypeError, ValueError):
        criterion = SortCriterion.FILE_TYPE_AND_NAME
    try:
        direction = SortDirection(settings.get("sort_direction"))
    except (TypeError, ValueError):
        direction = SortDirection.ASCENDING
    settings["sort_criterion"] = criterion.value
    settings["sort_direction"] = direction.value


def get_sorting_preferences(config: dict[str, Any]) -> SortPreferences:
    """Return validated preferences, using defaults for missing or invalid values."""
    settings = config.get("global_settings")
    if not isinstance(settings, dict):
        raise ConfigError("Configuration global_settings must be an object.")
    try:
        criterion = SortCriterion(settings.get("sort_criterion"))
    except (TypeError, ValueError):
        criterion = SortCriterion.FILE_TYPE_AND_NAME
    try:
        direction = SortDirection(settings.get("sort_direction"))
    except (TypeError, ValueError):
        direction = SortDirection.ASCENDING
    return SortPreferences(
        criterion,
        direction,
    )


def set_sorting_preferences(
    config: dict[str, Any], preferences: SortPreferences
) -> None:
    """Set validated persistent sorting choices in a config before saving it."""
    if not isinstance(preferences, SortPreferences):
        raise TypeError("preferences must be SortPreferences")
    settings = config.get("global_settings")
    if not isinstance(settings, dict):
        raise ConfigError("Configuration global_settings must be an object.")
    settings["sort_criterion"] = preferences.criterion.value
    settings["sort_direction"] = preferences.direction.value


def get_auto_clean_enabled(config: dict[str, Any]) -> bool:
    """Return the permanent post-processing cleanup setting, defaulting off."""
    return _get_cleanup_setting(config, "auto_clean")


def set_auto_clean_enabled(config: dict[str, Any], enabled: bool) -> None:
    _set_cleanup_setting(config, "auto_clean", enabled)


def get_auto_clear_trash_on_exit(config: dict[str, Any]) -> bool:
    """Return the independent normal-exit Trash cleanup setting, defaulting off."""
    return _get_cleanup_setting(config, "auto_clear_trash_on_exit")


def set_auto_clear_trash_on_exit(config: dict[str, Any], enabled: bool) -> None:
    _set_cleanup_setting(config, "auto_clear_trash_on_exit", enabled)


def _get_cleanup_setting(config: dict[str, Any], key: str) -> bool:
    settings = config.get("global_settings")
    if not isinstance(settings, dict):
        raise ConfigError("Configuration global_settings must be an object.")
    value = settings.get(key, False)
    if type(value) is not bool:
        raise ConfigError(f"Configuration {key} must be a boolean.")
    return value


def _set_cleanup_setting(config: dict[str, Any], key: str, enabled: bool) -> None:
    if type(enabled) is not bool:
        raise TypeError("enabled must be a boolean")
    settings = config.get("global_settings")
    if not isinstance(settings, dict):
        raise ConfigError("Configuration global_settings must be an object.")
    settings[key] = enabled


def load_config(path: Path | str) -> dict[str, Any]:
    """Load a valid config, or fresh defaults when the file does not exist."""
    config_path = Path(path)
    try:
        content = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_config()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read configuration at {config_path}: {exc}") from exc

    try:
        loaded = _validate_config(json.loads(content))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Malformed configuration at {config_path}: {exc}") from exc
    _normalize_sorting_settings(loaded["global_settings"])
    return loaded


def save_config(path: Path | str, config: dict[str, Any]) -> None:
    """Validate and atomically replace a config file, surfacing every failure."""
    config_path = Path(path)
    normalized = _validate_config(deepcopy(config))
    _normalize_sorting_settings(normalized["global_settings"])
    if config_path.exists():
        load_config(config_path)
    try:
        serialized = json.dumps(normalized, indent=2, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Configuration cannot be serialized: {exc}") from exc

    temporary_path: str | None = None
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary_file:
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, config_path)
    except OSError as exc:
        raise ConfigError(f"Cannot save configuration at {config_path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
