"""Thread-safe rotating application logging."""

from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
_CONFIGURE_LOCK = threading.Lock()
_HANDLER_MARKER = "_tidyfiles_managed_handler"


def configure_logging(
    log_path: Path | str | None,
    *,
    logger_name: str = "tidyfiles",
    max_bytes: int = MAX_LOG_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> logging.Logger:
    """Configure one rotating file handler, falling back to a no-op handler."""
    if max_bytes <= 0 or backup_count < 0:
        raise ValueError("max_bytes must be positive and backup_count cannot be negative.")

    path = Path(log_path).resolve() if log_path is not None else None
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    with _CONFIGURE_LOCK:
        managed = [
            handler
            for handler in logger.handlers
            if getattr(handler, _HANDLER_MARKER, False)
        ]
        if len(managed) == 1:
            handler = managed[0]
            configuration = (path, max_bytes, backup_count)
            if (
                isinstance(handler, RotatingFileHandler)
                and getattr(handler, "_tidyfiles_configuration", None) == configuration
            ):
                return logger

        for handler in managed:
            logger.removeHandler(handler)
            handler.close()

        try:
            handler: logging.Handler = (
                RotatingFileHandler(
                    path,
                    maxBytes=max_bytes,
                    backupCount=backup_count,
                    encoding="utf-8",
                )
                if path is not None
                else logging.NullHandler()
            )
        except OSError:
            handler = logging.NullHandler()

        setattr(handler, _HANDLER_MARKER, True)
        setattr(handler, "_tidyfiles_configuration", (path, max_bytes, backup_count))
        if isinstance(handler, RotatingFileHandler):
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
        logger.addHandler(handler)
    return logger
