import os
import subprocess
import sys
from pathlib import Path

from tidyfiles import startup
from tidyfiles.storage import StorageLocation


class _FakeLogger:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def info(self, _message: str, *_args: object) -> None:
        self.events.append("log startup")

    def exception(self, _message: str) -> None:
        self.events.append("log exception")


class _FakeGuard:
    def __init__(self, events: list[str], acquired: bool = True) -> None:
        self.events = events
        self.acquired = acquired

    def acquire(self) -> bool:
        self.events.append("acquire")
        return self.acquired

    def close(self) -> None:
        self.events.append("close")


def test_importing_entry_point_does_not_write_app_data(tmp_path: Path) -> None:
    source_directory = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(source_directory), environment.get("PYTHONPATH")))
    )

    subprocess.run(
        [sys.executable, "-c", "import tidyfiles.__main__"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )

    assert list(tmp_path.iterdir()) == []


def test_startup_acquires_guard_before_foundations_and_releases_after_gui(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[str] = []
    logger = _FakeLogger(events)
    location = StorageLocation(tmp_path, used_fallback=False)

    monkeypatch.setattr(startup, "SingleInstanceGuard", lambda: _FakeGuard(events))
    monkeypatch.setattr(
        startup,
        "resolve_storage_location",
        lambda: events.append("storage") or location,
    )
    monkeypatch.setattr(
        startup,
        "configure_logging",
        lambda path: events.append("logging") or logger,
    )
    monkeypatch.setattr(
        startup,
        "load_config",
        lambda path: events.append("config") or {},
    )
    monkeypatch.setattr(
        startup,
        "_launch_gui",
        lambda _config, _path: events.append("gui closed"),
    )

    assert startup.main() == 0
    assert events == [
        "acquire",
        "storage",
        "logging",
        "config",
        "log startup",
        "gui closed",
        "close",
    ]


def test_second_instance_stops_before_storage_or_logging(monkeypatch) -> None:
    events: list[str] = []
    errors: list[str] = []
    monkeypatch.setattr(
        startup,
        "SingleInstanceGuard",
        lambda: _FakeGuard(events, acquired=False),
    )
    monkeypatch.setattr(startup, "_show_startup_error", errors.append)
    monkeypatch.setattr(
        startup,
        "resolve_storage_location",
        lambda: (_ for _ in ()).throw(AssertionError("storage initialized")),
    )
    monkeypatch.setattr(
        startup,
        "configure_logging",
        lambda _path: (_ for _ in ()).throw(AssertionError("logging initialized")),
    )

    assert startup.main() == 1
    assert events == ["acquire", "close"]
    assert errors == ["Another TidyFiles instance is already running."]


def test_damaged_configuration_is_reported_without_rewriting_it(
    tmp_path: Path, monkeypatch
) -> None:
    events: list[str] = []
    errors: list[str] = []
    location = StorageLocation(tmp_path, used_fallback=False)
    config_path = location.config_path
    config_path.write_text("{ damaged", encoding="utf-8")
    original = config_path.read_bytes()

    monkeypatch.setattr(startup, "SingleInstanceGuard", lambda: _FakeGuard(events))
    monkeypatch.setattr(startup, "resolve_storage_location", lambda: location)
    monkeypatch.setattr(
        startup,
        "configure_logging",
        lambda _path: _FakeLogger(events),
    )
    monkeypatch.setattr(startup, "_show_startup_error", errors.append)
    monkeypatch.setattr(
        startup,
        "_launch_gui",
        lambda: (_ for _ in ()).throw(AssertionError("GUI launched")),
    )

    assert startup.main() == 1
    assert events == ["acquire", "log exception", "close"]
    assert config_path.read_bytes() == original
    assert len(errors) == 1
    assert "Malformed configuration" in errors[0]
