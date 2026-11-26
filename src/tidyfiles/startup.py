"""Single-instance application startup and foundation initialization."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from tidyfiles.config import load_config
from tidyfiles.instance import SingleInstanceGuard
from tidyfiles.logging_setup import configure_logging
from tidyfiles.storage import resolve_storage_location


def _show_startup_error(message: str) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, "TidyFiles", 0x10)
            return
        except (AttributeError, OSError):
            pass
    try:
        print(f"TidyFiles: {message}", file=sys.stderr)
    except (OSError, AttributeError):
        pass


def _launch_gui(config: dict[str, object], config_path: Path) -> None:
    from tidyfiles.app import create_main_window

    create_main_window(config, config_path).mainloop()


def main() -> int:
    guard = SingleInstanceGuard()
    logger = None
    exit_code = 0

    try:
        if not guard.acquire():
            _show_startup_error("Another TidyFiles instance is already running.")
            exit_code = 1
        else:
            location = resolve_storage_location()
            logger = configure_logging(location.log_path)
            config = load_config(location.config_path)
            logger.info("Starting TidyFiles using storage at %s", location.directory)
            _launch_gui(config, location.config_path)
    except Exception as exc:
        if logger is not None:
            logger.exception("TidyFiles startup failed")
        _show_startup_error(f"TidyFiles could not start.\n\n{exc}")
        exit_code = 1
    finally:
        try:
            guard.close()
        except Exception as exc:
            if logger is not None:
                logger.exception("TidyFiles could not release its instance guard")
            _show_startup_error(f"TidyFiles could not release its instance guard.\n\n{exc}")
            exit_code = 1

    return exit_code
