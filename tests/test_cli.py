from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "dbk.cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_collect_health_mock() -> None:
    proc = _run("collect", "health", "--source", "mock")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["details"]["collector"] == "mock"


def test_collect_health_pgstat_requires_dsn() -> None:
    proc = _run("collect", "health", "--source", "pgstat")
    assert proc.returncode == 2
    assert "Missing DSN" in proc.stderr


def test_cleanup_report_invalid_window_hours() -> None:
    proc = _run("runtime", "cleanup-report", "--window-hours", "0")
    assert proc.returncode == 2
    assert "window_hours must be > 0" in proc.stderr
