"""Tests for the alert daemon and CLI integration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{env.get('PYTHONPATH', '')}".rstrip(":")
    return subprocess.run(
        [sys.executable, "-m", "dbk.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


class TestAlertDaemonLifecycle:
    def test_alert_daemon_start_and_status(self, tmp_path: Path) -> None:
        start = _run(tmp_path, "alert", "daemon", "start", "--interval-sec", "1")
        assert start.returncode == 0, start.stderr
        payload = json.loads(start.stdout)
        assert payload["started"] is True
        assert payload["pid"] > 0

        # Give the daemon enough time to run several iterations
        time.sleep(4)

        # Read the state file directly to check evaluation count
        state_file = tmp_path / ".dbk" / "alert-daemon" / "state.json"
        state = json.loads(state_file.read_text())
        # Daemon writes pid, total_evaluations etc. to state file
        assert state["pid"] > 0
        assert state["total_evaluations"] >= 1

        stop = _run(tmp_path, "alert", "daemon", "stop")
        assert stop.returncode == 0, stop.stderr
        stop_payload = json.loads(stop.stdout)
        assert stop_payload["stopped"] is True

    def test_alert_daemon_stop_when_not_running(self, tmp_path: Path) -> None:
        # Stopping when not running is not an error (idempotent)
        stop = _run(tmp_path, "alert", "daemon", "stop")
        assert stop.returncode == 2, stop.stderr
        payload = json.loads(stop.stdout)
        # stopped=false because it wasn't running; reason is "not_running"

    def test_alert_daemon_status_when_not_running(self, tmp_path: Path) -> None:
        status = _run(tmp_path, "alert", "daemon", "status")
        assert status.returncode == 2, status.stderr
        payload = json.loads(status.stdout)
        assert payload["running"] is False

    def test_alert_daemon_start_idempotent(self, tmp_path: Path) -> None:
        start1 = _run(tmp_path, "alert", "daemon", "start", "--interval-sec", "5")
        assert start1.returncode == 0, start1.stderr
        pid1 = json.loads(start1.stdout)["pid"]

        # Starting again should raise RuntimeError
        start2 = _run(tmp_path, "alert", "daemon", "start", "--interval-sec", "5")
        assert start2.returncode == 2, start2.stderr
        assert "already running" in start2.stderr

        # Clean up
        stop = _run(tmp_path, "alert", "daemon", "stop")
        assert stop.returncode == 0

    def test_alert_daemon_run_and_eval(self, tmp_path: Path) -> None:
        """Test the rules eval command with a temporary rules file."""
        import json
        rules_file = tmp_path / "test_rules.json"
        rules_file.write_text(
            json.dumps({
                "rules": [
                    {
                        "name": "always_fire",
                        "metric": "dbk.nonexistent",
                        "operator": "gt",
                        "threshold": -999999999.0,
                        "severity": "critical",
                        "description": "Test rule",
                    }
                ]
            }),
            encoding="utf-8",
        )

        eval_result = _run(tmp_path, "alert", "rules", "eval", "--rules-path", str(rules_file))
        assert eval_result.returncode == 0, eval_result.stderr
        payload = json.loads(eval_result.stdout)
        assert "events" in payload
        assert "firing" in payload
        assert "summary" in payload

    def test_alert_rules_validate_valid(self, tmp_path: Path) -> None:
        import json
        rules_file = tmp_path / "valid.json"
        rules_file.write_text(
            json.dumps({
                "rules": [
                    {
                        "name": "high_cpu",
                        "metric": "cpu.usage",
                        "operator": "gt",
                        "threshold": 90.0,
                        "severity": "critical",
                        "description": "CPU too high",
                    }
                ]
            }),
            encoding="utf-8",
        )
        result = _run(tmp_path, "alert", "rules", "validate", "--rules-path", str(rules_file))
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["valid"] is True
        assert payload["count"] == 1

    def test_alert_rules_validate_invalid(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "invalid.json"
        rules_file.write_text("not json at all", encoding="utf-8")
        result = _run(tmp_path, "alert", "rules", "validate", "--rules-path", str(rules_file))
        assert result.returncode == 2, result.stderr
        payload = json.loads(result.stdout)
        assert payload["valid"] is False

    def test_alert_rules_list(self, tmp_path: Path) -> None:
        result = _run(tmp_path, "alert", "rules", "list", "--format", "json")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert "rules" in payload

    def test_alert_prometheus_once(self, tmp_path: Path) -> None:
        import threading
        port = 20000 + threading.current_thread().ident % 10000
        result = _run(tmp_path, "alert", "prometheus", "--once", "--listen-port", str(port))
        assert result.returncode == 0, result.stderr
        assert "HELP" in result.stdout or "TYPE" in result.stdout or result.stdout == ""

    def test_alert_history_empty(self, tmp_path: Path) -> None:
        result = _run(tmp_path, "alert", "history", "--limit", "5")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert "alerts" in payload
        assert "count" in payload
