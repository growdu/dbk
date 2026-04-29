from __future__ import annotations

import os
from pathlib import Path

import pytest

from dbk.collector_daemon import daemon_status, is_pid_running, stop_daemon
from dbk.config import collector_daemon_state_path


def test_is_pid_running_current_process() -> None:
    assert is_pid_running(os.getpid()) is True


def test_daemon_status_not_running(tmp_path: Path) -> None:
    payload = daemon_status(cwd=tmp_path)
    assert payload["running"] is False
    assert payload["daemons"] == []


def test_stop_daemon_not_running(tmp_path: Path) -> None:
    payload = stop_daemon(cwd=tmp_path)
    assert payload["stopped"] is False
    assert payload["reason"] == "not_running"


def test_stop_daemon_permission_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = collector_daemon_state_path(tmp_path, instance="default")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        '{"pid": 12345, "instance": "default", "source": "mock", "interval_sec": 1, "started_at": "t", "log_path": "p"}',
        encoding="utf-8",
    )

    monkeypatch.setattr("dbk.collector_daemon.is_pid_running", lambda _pid: True)

    def _kill(_pid: int, _sig: int) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("os.kill", _kill)
    payload = stop_daemon(cwd=tmp_path)
    assert payload["stopped"] is False
    assert payload["reason"] == "permission_denied_on_sigterm"
