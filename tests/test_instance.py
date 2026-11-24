import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from tidyfiles.instance import (
    DEFAULT_MUTEX_NAME,
    InstanceAlreadyRunningError,
    InstanceGuardError,
    SingleInstanceGuard,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex tests")

_OWNER_SCRIPT = """
import pathlib, sys, time
from tidyfiles.instance import SingleInstanceGuard
guard = SingleInstanceGuard(sys.argv[1])
if not guard.acquire():
    raise SystemExit(2)
pathlib.Path(sys.argv[2]).write_text('ready', encoding='utf-8')
while not pathlib.Path(sys.argv[3]).exists():
    time.sleep(0.02)
guard.close()
"""
_CONTENDER_SCRIPT = """
import sys
from tidyfiles.instance import SingleInstanceGuard
guard = SingleInstanceGuard(sys.argv[1])
acquired = guard.acquire()
print('acquired' if acquired else 'already-running')
if acquired:
    guard.close()
"""


def _mutex_name() -> str:
    return rf"Local\TidyFiles.Tests.{uuid.uuid4().hex}"


def _start_owner(tmp_path: Path, mutex_name: str) -> tuple[subprocess.Popen[str], Path, Path]:
    ready_path = tmp_path / f"{uuid.uuid4().hex}.ready"
    stop_path = tmp_path / f"{uuid.uuid4().hex}.stop"
    process = subprocess.Popen(
        [sys.executable, "-c", _OWNER_SCRIPT, mutex_name, str(ready_path), str(stop_path)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready_path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    if not ready_path.exists():
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                process.terminate()
            stdout, stderr = process.communicate(timeout=5)
        pytest.fail(f"Mutex owner did not start. stdout={stdout!r}, stderr={stderr!r}")
    return process, ready_path, stop_path


def _stop_owner(
    process: subprocess.Popen[str], stop_path: Path, *, terminate: bool = False
) -> None:
    if process.poll() is None:
        if terminate:
            process.terminate()
        else:
            stop_path.write_text("stop", encoding="utf-8")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


def test_default_mutex_name_is_stable_across_working_directories() -> None:
    assert DEFAULT_MUTEX_NAME == r"Local\TidyFiles.SingleInstance"
    assert SingleInstanceGuard().mutex_name == DEFAULT_MUTEX_NAME


def test_initial_acquisition_and_second_acquisition_rejection() -> None:
    mutex_name = _mutex_name()
    first = SingleInstanceGuard(mutex_name)
    second = SingleInstanceGuard(mutex_name)
    try:
        assert first.acquire() is True
        assert first.acquired
        assert second.acquire() is False
        assert not second.acquired
    finally:
        first.close()
        second.close()


def test_acquisition_succeeds_after_orderly_release() -> None:
    mutex_name = _mutex_name()
    first = SingleInstanceGuard(mutex_name)
    second = SingleInstanceGuard(mutex_name)
    assert first.acquire()
    first.close()
    try:
        assert second.acquire()
    finally:
        second.close()


def test_cleanup_is_idempotent() -> None:
    guard = SingleInstanceGuard(_mutex_name())
    assert guard.acquire()
    guard.close()
    guard.close()
    assert not guard.acquired


def test_ownership_is_held_until_guard_closes() -> None:
    mutex_name = _mutex_name()
    owner = SingleInstanceGuard(mutex_name)
    contender = SingleInstanceGuard(mutex_name)
    try:
        assert owner.acquire()
        assert contender.acquire() is False
        owner.close()
        assert contender.acquire()
    finally:
        owner.close()
        contender.close()


def test_context_manager_distinguishes_existing_instance() -> None:
    mutex_name = _mutex_name()
    owner = SingleInstanceGuard(mutex_name)
    assert owner.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunningError):
            with SingleInstanceGuard(mutex_name):
                pass
    finally:
        owner.close()


def test_context_manager_releases_when_body_raises() -> None:
    mutex_name = _mutex_name()
    with pytest.raises(RuntimeError, match="test exception"):
        with SingleInstanceGuard(mutex_name):
            raise RuntimeError("test exception")

    guard = SingleInstanceGuard(mutex_name)
    try:
        assert guard.acquire()
    finally:
        guard.close()


def test_independent_subprocesses_share_the_mutex(tmp_path: Path) -> None:
    mutex_name = _mutex_name()
    owner, _ready_path, stop_path = _start_owner(tmp_path, mutex_name)
    try:
        contender = subprocess.run(
            [sys.executable, "-c", _CONTENDER_SCRIPT, mutex_name],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert contender.stdout.strip() == "already-running"

        stop_path.write_text("stop", encoding="utf-8")
        assert owner.wait(timeout=5) == 0
        next_contender = subprocess.run(
            [sys.executable, "-c", _CONTENDER_SCRIPT, mutex_name],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert next_contender.stdout.strip() == "acquired"
    finally:
        _stop_owner(owner, stop_path)


def test_abnormal_subprocess_termination_releases_mutex(tmp_path: Path) -> None:
    mutex_name = _mutex_name()
    owner, _ready_path, stop_path = _start_owner(tmp_path, mutex_name)
    owner.terminate()
    assert owner.wait(timeout=5) is not None

    guard = SingleInstanceGuard(mutex_name)
    try:
        assert guard.acquire()
    finally:
        guard.close()
        _stop_owner(owner, stop_path)


def test_rejection_does_not_create_configuration_or_log_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutex_name = _mutex_name()
    owner = SingleInstanceGuard(mutex_name)
    contender = SingleInstanceGuard(mutex_name)
    assert owner.acquire()
    monkeypatch.chdir(tmp_path)
    try:
        assert contender.acquire() is False
        assert not (tmp_path / "config.json").exists()
        assert not (tmp_path / "tidyfiles.log").exists()
    finally:
        owner.close()
        contender.close()


def test_unexpected_windows_api_failure_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKernel32:
        def CreateMutexW(self, _attributes, _initial_owner, _name):
            return None

    monkeypatch.setattr("tidyfiles.instance._load_kernel32", lambda: FakeKernel32())
    monkeypatch.setattr("tidyfiles.instance._last_error", lambda: 5)

    with pytest.raises(InstanceGuardError, match="CreateMutexW") as error:
        SingleInstanceGuard(_mutex_name()).acquire()

    assert error.value.error_code == 5
