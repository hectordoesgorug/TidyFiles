import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from tidyfiles.logging_setup import configure_logging


def _close_managed_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_tidyfiles_managed_handler", False):
            logger.removeHandler(handler)
            handler.close()


def test_logs_rotate_and_retain_configured_backups(tmp_path: Path) -> None:
    log_path = tmp_path / "tidyfiles.log"
    logger = configure_logging(log_path, logger_name="tidyfiles.test.rotation", max_bytes=160)
    try:
        for index in range(20):
            logger.info("operation=%s value=%s", "test", index)

        for handler in logger.handlers:
            handler.flush()
        log_files = list(tmp_path.glob("tidyfiles.log*"))

        assert log_path in log_files
        assert log_path.with_name("tidyfiles.log.1") in log_files
        assert len(log_files) <= 4
        assert all(file.stat().st_size <= 160 for file in log_files)
    finally:
        _close_managed_handlers(logger)


def test_repeated_initialization_does_not_duplicate_messages(tmp_path: Path) -> None:
    log_path = tmp_path / "tidyfiles.log"
    logger = configure_logging(log_path, logger_name="tidyfiles.test.once")
    try:
        same_logger = configure_logging(log_path, logger_name="tidyfiles.test.once")
        logger.info("one operation")
        for handler in logger.handlers:
            handler.flush()

        assert same_logger is logger
        rotating_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating_handlers) == 1
        assert rotating_handlers[0].maxBytes == 5 * 1024 * 1024
        assert rotating_handlers[0].backupCount == 3
        assert log_path.read_text(encoding="utf-8").count("one operation") == 1
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO", log_path.read_text())
    finally:
        _close_managed_handlers(logger)


def test_unavailable_log_storage_does_not_raise_on_logging(tmp_path: Path) -> None:
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("occupied", encoding="utf-8")
    logger = configure_logging(
        parent_file / "tidyfiles.log", logger_name="tidyfiles.test.unavailable"
    )
    try:
        logger.info("operation continues")
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.NullHandler)
    finally:
        _close_managed_handlers(logger)
