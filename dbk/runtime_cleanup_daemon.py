from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collector_daemon import is_pid_running
from .config import (
    runtime_cleanup_daemon_log_path,
    runtime_cleanup_daemon_state_path,
    runtime_cleanup_history_path,
    runtime_db_path,
)
from .models import utc_now_iso
from .runtime_cleanup import cleanup_runtime_data
from .storage import RuntimeStore


@dataclass(slots=True)
class RuntimeCleanupDaemonState:
    pid: int
    interval_sec: int
    older_than_hours: float
    instance: str | None
    skip_trace_db: bool
    skip_artifacts: bool
    vacuum: bool
    started_at: str
    log_path: str
    last_heartbeat_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    last_summary: dict[str, Any] | None = None
    total_runs: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "interval_sec": self.interval_sec,
            "older_than_hours": self.older_than_hours,
            "instance": self.instance,
            "skip_trace_db": self.skip_trace_db,
            "skip_artifacts": self.skip_artifacts,
            "vacuum": self.vacuum,
            "started_at": self.started_at,
            "log_path": self.log_path,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "last_summary": self.last_summary,
            "total_runs": self.total_runs,
        }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _append_history_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


def read_cleanup_history(*, limit: int = 50, cwd: Path | None = None) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    history_path = runtime_cleanup_history_path(cwd)
    if not history_path.exists():
        return []
    lines = history_path.read_text(encoding="utf-8").splitlines()
    selected = lines[-limit:]
    payload: list[dict[str, Any]] = []
    for line in selected:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                payload.append(parsed)
        except json.JSONDecodeError:
            continue
    return payload


def build_cleanup_report(*, limit: int = 50, cwd: Path | None = None) -> dict[str, Any]:
    history = read_cleanup_history(limit=limit, cwd=cwd)
    total_runs = len(history)
    total_metrics_deleted = 0
    total_trace_deleted = 0
    total_artifact_dirs_deleted = 0
    failed_runs = 0
    for item in history:
        error = item.get("error")
        if error:
            failed_runs += 1
            continue
        summary = item.get("summary", {})
        if isinstance(summary, dict):
            runtime_metrics = summary.get("runtime_metrics", {})
            trace_db = summary.get("trace_artifacts_db", {})
            artifact_dirs = summary.get("artifact_dirs", {})
            if isinstance(runtime_metrics, dict):
                total_metrics_deleted += int(runtime_metrics.get("deleted", 0) or 0)
            if isinstance(trace_db, dict):
                total_trace_deleted += int(trace_db.get("deleted", 0) or 0)
            if isinstance(artifact_dirs, dict):
                total_artifact_dirs_deleted += int(artifact_dirs.get("deleted", 0) or 0)

    last_run_at = history[-1].get("ts") if history else None
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    return {
        "generated_at": now_iso,
        "window_size": limit,
        "total_runs": total_runs,
        "failed_runs": failed_runs,
        "last_run_at": last_run_at,
        "totals": {
            "runtime_metrics_deleted": total_metrics_deleted,
            "trace_artifacts_deleted": total_trace_deleted,
            "artifact_dirs_deleted": total_artifact_dirs_deleted,
        },
        "recent": history[-10:],
    }


def read_state(path: Path | None = None) -> dict[str, object] | None:
    state_path = path or runtime_cleanup_daemon_state_path()
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def start_cleanup_daemon(
    *,
    interval_sec: int,
    older_than_hours: float,
    instance: str | None,
    skip_trace_db: bool,
    skip_artifacts: bool,
    vacuum: bool,
    cwd: Path | None = None,
) -> RuntimeCleanupDaemonState:
    if interval_sec <= 0:
        raise ValueError("interval_sec must be > 0")
    if older_than_hours <= 0:
        raise ValueError("older_than_hours must be > 0")

    state_path = runtime_cleanup_daemon_state_path(cwd)
    log_path = runtime_cleanup_daemon_log_path(cwd)
    existing = read_state(state_path)
    if existing and is_pid_running(int(existing["pid"])):
        raise RuntimeError(f"runtime cleanup daemon already running with pid={existing['pid']}")

    cmd = [
        sys.executable,
        "-m",
        "dbk.cli",
        "runtime",
        "cleanup-daemon",
        "run",
        "--interval-sec",
        str(interval_sec),
        "--older-than-hours",
        str(older_than_hours),
        "--state-path",
        str(state_path),
    ]
    if instance:
        cmd.extend(["--instance", instance])
    if skip_trace_db:
        cmd.append("--skip-trace-db")
    if skip_artifacts:
        cmd.append("--skip-artifacts")
    if vacuum:
        cmd.append("--vacuum")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as logf:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(cwd or Path.cwd()),
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )

    state = RuntimeCleanupDaemonState(
        pid=proc.pid,
        interval_sec=interval_sec,
        older_than_hours=older_than_hours,
        instance=instance,
        skip_trace_db=skip_trace_db,
        skip_artifacts=skip_artifacts,
        vacuum=vacuum,
        started_at=utc_now_iso(),
        log_path=str(log_path),
    )
    _write_json(state_path, state.to_dict())
    return state


def stop_cleanup_daemon(*, cwd: Path | None = None, timeout_sec: float = 5.0) -> dict[str, object]:
    state_path = runtime_cleanup_daemon_state_path(cwd)
    state = read_state(state_path)
    if state is None:
        return {"stopped": False, "reason": "not_running"}

    pid = int(state["pid"])
    if not is_pid_running(pid):
        state_path.unlink(missing_ok=True)
        return {"stopped": True, "pid": pid, "signal": "none"}
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        return {"stopped": False, "pid": pid, "reason": "permission_denied_on_sigterm"}

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not is_pid_running(pid):
            state_path.unlink(missing_ok=True)
            return {"stopped": True, "pid": pid, "signal": "SIGTERM"}
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except PermissionError:
        return {"stopped": False, "pid": pid, "reason": "permission_denied_on_sigkill"}
    state_path.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid, "signal": "SIGKILL"}


def cleanup_daemon_status(*, cwd: Path | None = None) -> dict[str, object]:
    state_path = runtime_cleanup_daemon_state_path(cwd)
    state = read_state(state_path)
    if state is None:
        return {"running": False}
    payload = dict(state)
    payload["running"] = is_pid_running(int(state["pid"]))
    payload["state_path"] = str(state_path)
    return payload


def run_cleanup_loop(
    *,
    interval_sec: int,
    older_than_hours: float,
    instance: str | None,
    skip_trace_db: bool,
    skip_artifacts: bool,
    vacuum: bool,
    state_path: Path | None = None,
    history_path: Path | None = None,
) -> int:
    target_state_path = state_path or runtime_cleanup_daemon_state_path()
    target_history_path = history_path or runtime_cleanup_history_path()
    target_state = read_state(target_state_path) or {}

    state = RuntimeCleanupDaemonState(
        pid=os.getpid(),
        interval_sec=int(target_state.get("interval_sec", interval_sec)),
        older_than_hours=float(target_state.get("older_than_hours", older_than_hours)),
        instance=target_state.get("instance"),  # type: ignore[arg-type]
        skip_trace_db=bool(target_state.get("skip_trace_db", skip_trace_db)),
        skip_artifacts=bool(target_state.get("skip_artifacts", skip_artifacts)),
        vacuum=bool(target_state.get("vacuum", vacuum)),
        started_at=str(target_state.get("started_at", utc_now_iso())),
        log_path=str(target_state.get("log_path", runtime_cleanup_daemon_log_path())),
        last_heartbeat_at=target_state.get("last_heartbeat_at"),  # type: ignore[arg-type]
        last_success_at=target_state.get("last_success_at"),  # type: ignore[arg-type]
        last_error=target_state.get("last_error"),  # type: ignore[arg-type]
        last_summary=target_state.get("last_summary"),  # type: ignore[arg-type]
        total_runs=int(target_state.get("total_runs", 0)),
    )

    stop_flag = {"stop": False}

    def _handle_signal(_sig: int, _frame: object) -> None:
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not stop_flag["stop"]:
        now = utc_now_iso()
        state.last_heartbeat_at = now
        try:
            store = RuntimeStore(runtime_db_path())
            store.init_schema()
            summary = cleanup_runtime_data(
                store=store,
                older_than_hours=state.older_than_hours,
                instance=state.instance,
                dry_run=False,
                skip_trace_db=state.skip_trace_db,
                skip_artifacts=state.skip_artifacts,
                vacuum=state.vacuum,
            )
            state.total_runs += 1
            state.last_success_at = now
            state.last_error = None
            summary_payload = summary.to_dict()
            state.last_summary = summary_payload
            _append_history_event(
                target_history_path,
                {
                    "ts": now,
                    "ok": True,
                    "summary": summary_payload,
                },
            )
        except Exception as exc:  # pragma: no cover
            state.last_error = str(exc)
            _append_history_event(
                target_history_path,
                {
                    "ts": now,
                    "ok": False,
                    "error": str(exc),
                },
            )
        _write_json(target_state_path, state.to_dict())
        for _ in range(state.interval_sec):
            if stop_flag["stop"]:
                break
            time.sleep(1)
    return 0
