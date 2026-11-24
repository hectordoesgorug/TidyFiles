"""Versioned configuration loading and atomic persistence."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
_DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "global_settings": {
        "auto_clean": False,
        "auto_clear_trash_on_exit": False,
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
    profiles = config.get("profiles")
    if not isinstance(profiles, dict):
        raise ConfigError("Configuration profiles must be an object.")
    if any(
        not isinstance(name, str) or not isinstance(profile, dict)
        for name, profile in profiles.items()
    ):
        raise ConfigError("Each named profile must be an object with a string name.")
    return config


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
        return _validate_config(json.loads(content))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Malformed configuration at {config_path}: {exc}") from exc


def save_config(path: Path | str, config: dict[str, Any]) -> None:
    """Validate and atomically replace a config file, surfacing every failure."""
    config_path = Path(path)
    _validate_config(config)
    if config_path.exists():
        load_config(config_path)
    try:
        serialized = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
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
