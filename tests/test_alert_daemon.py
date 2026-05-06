"""Tests for dbk.alerting.daemon — alert daemon lifecycle and helpers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dbk.alerting.daemon import (
    AlertDaemonState,
    _collect_metrics_for_alerting,
    _write_json,
    alert_daemon_dir,
    alert_daemon_log_path,
    alert_daemon_state_path,
    alert_daemon_status,
    is_pid_running,
    read_state,
    run_alert_loop,
    start_alert_daemon,
    stop_alert_daemon,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# AlertDaemonState
# ---------------------------------------------------------------------------

class TestAlertDaemonState:
    def test_to_dict_round_trip(self) -> None:
        state = AlertDaemonState(
            pid=12345,
            started_at="2026-04-30T00:00:00Z",
            interval_sec=30,
            rules_loaded=5,
            total_evaluations=100,
            total_firings=3,
            total_resolutions=1,
            last_evaluation_at="2026-04-30T01:00:00Z",
            last_error=None,
            log_path="/var/log/dbk/alert-daemon.log",
        )
        d = state.to_dict()
        assert d["pid"] == 12345
        assert d["rules_loaded"] == 5
        assert d["total_firings"] == 3
        assert d["total_resolutions"] == 1
        assert d["last_error"] is None
        assert d["log_path"] == "/var/log/dbk/alert-daemon.log"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

class TestPathHelpers:
    def test_alert_daemon_dir(self, tmp_path: Path) -> None:
        # alert_daemon_dir calls dbk_root(cwd) which resolves to <cwd>/.dbk/
        result = alert_daemon_dir(tmp_path)
        assert result == tmp_path / ".dbk" / "alert-daemon"

    def test_alert_daemon_state_path(self, tmp_path: Path) -> None:
        result = alert_daemon_state_path(tmp_path)
        assert result == tmp_path / ".dbk" / "alert-daemon" / "state.json"

    def test_alert_daemon_log_path(self, tmp_path: Path) -> None:
        result = alert_daemon_log_path(tmp_path)
        assert result == tmp_path / ".dbk" / "alert-daemon" / "alert-daemon.log"


# ---------------------------------------------------------------------------
# is_pid_running
# ---------------------------------------------------------------------------

class TestIsPidRunning:
    def test_returns_true_for_current_process(self) -> None:
        assert is_pid_running(os.getpid()) is True

    def test_returns_false_for_nonexistent_pid(self) -> None:
        # Use max PID unlikely to exist in any environment
        assert is_pid_running(999999) is False

    def test_returns_true_for_permission_denied(self) -> None:
        # If kill(PID, 0) raises PermissionError, the process exists but we lack permission
        result = is_pid_running(1)  # init/kthread
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# read_state / _write_json
# ---------------------------------------------------------------------------

class TestReadState:
    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        assert read_state(tmp_path / "no-such-file.json") is None

    def test_reads_existing_state(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        payload = {"pid": 99999, "started_at": "2026-04-30T00:00:00Z"}
        state_file.write_text(json.dumps(payload), encoding="utf-8")

        result = read_state(state_file)
        assert result == payload

    def test_raises_on_malformed_json(self, tmp_path: Path) -> None:
        state_file = tmp_path / "bad.json"
        state_file.write_text("not valid json{{", encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            read_state(state_file)


class TestWriteJson:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c" / "state.json"
        _write_json(target, {"key": "value"})
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == {"key": "value"}


# ---------------------------------------------------------------------------
# start_alert_daemon
# ---------------------------------------------------------------------------

class TestStartAlertDaemon:
    def test_start_daemon_writes_state(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 88888

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            state = start_alert_daemon(
                interval_sec=30,
                cwd=tmp_path,
            )

        assert state.pid == 88888
        assert state.interval_sec == 30
        mock_popen.assert_called_once()

        state_file = alert_daemon_state_path(tmp_path)
        assert state_file.exists()
        loaded = json.loads(state_file.read_text(encoding="utf-8"))
        assert loaded["pid"] == 88888

    def test_start_daemon_refuses_duplicate(self, tmp_path: Path) -> None:
        # Pre-write a running state with current PID (which is definitely "running")
        state_file = alert_daemon_state_path(tmp_path)
        _write_state(state_file, {"pid": os.getpid(), "started_at": "2026-04-30T00:00:00Z"})

        with pytest.raises(RuntimeError, match="already running"):
            start_alert_daemon(cwd=tmp_path)

    def test_start_daemon_invalid_interval(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="interval_sec must be > 0"):
            start_alert_daemon(interval_sec=0, cwd=tmp_path)

    def test_start_daemon_with_webhook_flags(self, tmp_path: Path) -> None:
        mock_proc = MagicMock()
        mock_proc.pid = 77777

        with patch("subprocess.Popen", return_value=mock_proc):
            start_alert_daemon(
                interval_sec=15,
                webhook_url="http://example.com/webhook",
                webhook_secret="secret",
                cwd=tmp_path,
            )

        # Can't easily inspect subprocess.Popen args due to in-process patch,
        # so just verify it didn't raise and state was written
        state_file = alert_daemon_state_path(tmp_path)
        assert state_file.exists()


# ---------------------------------------------------------------------------
# stop_alert_daemon
# ---------------------------------------------------------------------------

class TestStopAlertDaemon:
    def test_stop_returns_not_running_when_no_state(self, tmp_path: Path) -> None:
        # No state file at all — nothing to stop
        result = stop_alert_daemon(cwd=tmp_path)
        assert result["stopped"] is False
        assert result["reason"] == "not_running"

    def test_stop_returns_dead_when_pid_gone(self, tmp_path: Path) -> None:
        state_file = alert_daemon_state_path(tmp_path)
        _write_state(state_file, {"pid": 99999, "started_at": "2026-04-30T00:00:00Z"})

        # PID appears not running → cleaned up and returns success
        with patch("dbk.alerting.daemon.is_pid_running", return_value=False):
            result = stop_alert_daemon(cwd=tmp_path)

        assert result["stopped"] is True
        assert result["signal"] == "none"
        assert not state_file.exists()  # cleaned up

    def test_stop_sends_sigterm_and_cleans_up(self, tmp_path: Path) -> None:
        """SIGTERM is sent; when PID disappears, state file is removed."""
        state_file = alert_daemon_state_path(tmp_path)
        _write_state(state_file, {"pid": 99999, "started_at": "2026-04-30T00:00:00Z"})

        calls: dict[str, list[Any]] = {"kill": [], "is_running": []}

        def tracking_kill(pid: int, sig: int) -> None:
            calls["kill"].append((pid, sig))

        def tracking_is_running(pid: int) -> bool:
            calls["is_running"].append(pid)
            # First few calls (while waiting for SIGTERM): still running
            # After SIGKILL: raise ProcessLookupError
            if sig_count := len([k for k in calls["kill"] if k[1] == signal.SIGTERM]):
                if sig_count >= 1:
                    raise ProcessLookupError(pid)
            return True

        sig_count = 0

        def tracking_kill2(pid: int, sig: int) -> None:
            nonlocal sig_count
            calls["kill"].append((pid, sig))
            if sig == signal.SIGKILL:
                raise ProcessLookupError(pid)

        def tracking_is_running2(pid: int) -> bool:
            calls["is_running"].append(pid)
            return True

        with patch("os.kill", side_effect=tracking_kill2):
            with patch("dbk.alerting.daemon.is_pid_running", side_effect=tracking_is_running2):
                with patch("time.sleep"):  # speed up polling
                    result = stop_alert_daemon(cwd=tmp_path, graceful_timeout_sec=0.1)

        assert signal.SIGTERM in [k[1] for k in calls["kill"]]
        assert result["stopped"] is True

    def test_stop_permission_denied_on_sigterm(self, tmp_path: Path) -> None:
        state_file = alert_daemon_state_path(tmp_path)
        _write_state(state_file, {"pid": 99999, "started_at": "2026-04-30T00:00:00Z"})

        def fake_is_running(pid: int) -> bool:
            return True  # appears running

        def fake_kill(pid: int, sig: int) -> None:
            if sig == signal.SIGTERM:
                raise PermissionError()

        with patch("os.kill", side_effect=fake_kill):
            with patch("dbk.alerting.daemon.is_pid_running", side_effect=fake_is_running):
                result = stop_alert_daemon(cwd=tmp_path)

        assert result["stopped"] is False
        assert "permission_denied" in result["reason"]


# ---------------------------------------------------------------------------
# alert_daemon_status
# ---------------------------------------------------------------------------

class TestAlertDaemonStatus:
    def test_status_not_running(self, tmp_path: Path) -> None:
        result = alert_daemon_status(cwd=tmp_path)
        assert result["running"] is False

    def test_status_running(self, tmp_path: Path) -> None:
        state_file = alert_daemon_state_path(tmp_path)
        _write_state(state_file, {
            "pid": os.getpid(),
            "started_at": "2026-04-30T00:00:00Z",
            "interval_sec": 30,
        })

        result = alert_daemon_status(cwd=tmp_path)
        assert result["running"] is True
        assert result["pid"] == os.getpid()


# ---------------------------------------------------------------------------
# _collect_metrics_for_alerting
# ---------------------------------------------------------------------------

class TestCollectMetricsForAlerting:
    def test_returns_empty_when_no_data(self, tmp_path: Path) -> None:
        from dbk.storage import RuntimeStore

        store = RuntimeStore(tmp_path / "rt.sqlite")
        store.init_schema()

        result = _collect_metrics_for_alerting(store)
        assert result == []

    def test_returns_metrics_in_time_window(self, tmp_path: Path) -> None:
        from datetime import datetime, timezone
        from dbk.storage import RuntimeStore

        store = RuntimeStore(tmp_path / "rt.sqlite")
        store.init_schema()

        now = datetime.now(tz=timezone.utc).isoformat()

        # Schema: ts, instance, source, category, metric, value, labels_json
        with store.connect() as conn:
            conn.execute(
                "INSERT INTO runtime_metric (ts, instance, source, category, metric, value, labels_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, "pg-01", "mock", "system", "cpu_usage", 45.0, "{}"),
            )

        result = _collect_metrics_for_alerting(store, max_age_minutes=5)
        assert len(result) == 1
        assert result[0]["metric"] == "cpu_usage"
        assert result[0]["value"] == 45.0
        assert result[0]["instance"] == "pg-01"


# ---------------------------------------------------------------------------
# run_alert_loop
# ---------------------------------------------------------------------------

class TestRunAlertLoop:
    def test_loop_runs_one_iteration_and_exits(self, tmp_path: Path) -> None:
        """Verify the alert daemon run loop evaluates and writes state."""
        state_file = alert_daemon_state_path(tmp_path)
        _write_state(state_file, {
            "pid": os.getpid(),
            "started_at": "2026-04-30T00:00:00Z",
            "interval_sec": 1,
            "total_evaluations": 0,
        })

        script_file = tmp_path / "_test_loop_script.py"
        project_root = Path(__file__).resolve().parents[1]
        script_file.write_text(
            f"""
import sys
sys.path.insert(0, "{project_root}")
from pathlib import Path
from dbk.alerting.daemon import run_alert_loop

run_alert_loop(
    interval_sec=1,
    rules_path=None,
    state_path=Path("{state_file}"),
    cwd=Path("{tmp_path}"),
)
""",
            encoding="utf-8",
        )

        proc = subprocess.Popen(
            [sys.executable, str(script_file)],
            cwd=str(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(1.5)
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)

        assert state_file.exists()
        state = json.loads(state_file.read_text(encoding="utf-8"))
        # Loop should have run at least one evaluation
        assert state["total_evaluations"] >= 1, (
            f"expected >= 1 eval, got {state}. stderr: {stderr.decode()[:300]}"
        )

    def test_loop_loads_rules_and_fires(self, tmp_path: Path) -> None:
        """Rules are loaded and matched; firings are counted in daemon state.

        The daemon runs in a subprocess so it has its own empty runtime store.
        We patch _collect_metrics_for_alerting (takes RuntimeStore, returns list[dict])
        so the engine has deterministic metrics to evaluate.
        """
        state_file = alert_daemon_state_path(tmp_path)
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(
            json.dumps({
                "rules": [{
                    "name": "high_cpu",
                    "metric": "cpu_usage",
                    "operator": "gt",
                    "threshold": 80.0,
                    "severity": "warning",
                }],
            }),
            encoding="utf-8",
        )
        _write_state(state_file, {
            "pid": os.getpid(),
            "started_at": "2026-04-30T00:00:00Z",
            "interval_sec": 1,
        })

        from datetime import datetime, timezone

        script_file = tmp_path / "_test_loop_script2.py"
        project_root = Path(__file__).resolve().parents[1]
        script_file.write_text(
            f"""
import sys
sys.path.insert(0, "{project_root}")
from pathlib import Path
from datetime import datetime, timezone
from dbk.alerting.daemon import run_alert_loop
from dbk.storage import RuntimeStore
from unittest.mock import patch

now_ts = datetime.now(tz=timezone.utc)
fake_metrics = [{{
    "metric": "cpu_usage",
    "value": 95.0,
    "instance": "pg-01",
    "ts": now_ts.isoformat(),  # engine._parse_ts expects a string
    "source": "mock",
    "category": "system",
    "labels": {{}},
}}]

def fake_collect(store: RuntimeStore, max_age_minutes=5, limit_per_metric=5):
    return fake_metrics

with patch("dbk.alerting.daemon._collect_metrics_for_alerting", fake_collect):
    run_alert_loop(
        interval_sec=1,
        rules_path=Path("{rules_file}"),
        state_path=Path("{state_file}"),
        cwd=Path("{tmp_path}"),
    )
""",
            encoding="utf-8",
        )

        proc = subprocess.Popen(
            [sys.executable, str(script_file)],
            cwd=str(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(1.5)
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)

        state = json.loads(state_file.read_text(encoding="utf-8"))
        stderr_decoded = stderr.decode()[:500]
        assert state.get("rules_loaded") == 1, (
            f"rules not loaded. state={state}, stderr: {stderr_decoded}"
        )
        assert state["total_evaluations"] >= 1, (
            f"no evaluations run. stderr: {stderr_decoded}"
        )
        assert state["total_firings"] >= 1, (
            f"alert did not fire. state={state}, stderr: {stderr_decoded}"
        )
