from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_collection_controls_in_isolated_tk_process() -> None:
    worker = Path(__file__).with_name("collection_controls_worker.py")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(worker)],
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
