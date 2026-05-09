"""Smoke tests for dbk CLI commands.

Commands now return CommandResult (code/data/message/details/warnings).
These tests adapt to the unified contract output format.
"""
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
    # collect health mock returns text by default; use --format json for structured data
    proc = _run("collect", "health", "--source", "mock", "--format", "json")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    # CommandResult wraps in "data"
    data = payload["data"] if "data" in payload else payload
    assert data["ok"] is True
    assert data["collector"] == "mock"


def test_collect_health_pgstat_requires_dsn() -> None:
    # pgstat without DSN returns CONFIG_ERROR (code=3)
    proc = _run("collect", "health", "--source", "pgstat")
    assert proc.returncode == 3  # CONFIG_ERROR


def test_cleanup_report_invalid_window_hours() -> None:
    # Invalid window-hours returns DATA_ERROR (code=5)
    proc = _run("runtime", "cleanup-report", "--window-hours", "0")
    assert proc.returncode == 5  # DATA_ERROR
