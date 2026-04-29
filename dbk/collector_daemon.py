from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from .config import collector_daemon_dir, collector_daemon_log_path, collector_daemon_state_path
from .models import utc_now_iso


@dataclass(slots=True)
class CollectorDaemonState:
    pid: int
    instance: str
    source: str
    interval_sec: int
    priority: int
    tags: list[str]
    max_collections_per_minute: int | None
    started_at: str
    log_path: str
    last_heartbeat_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    total_collections: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "instance": self.instance,
            "source": self.source,
            "interval_sec": self.interval_sec,
            "priority": self.priority,
            "tags": self.tags,
            "max_collections_per_minute": self.max_collections_per_minute,
            "started_at": self.started_at,
            "log_path": self.log_path,
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "total_collections": self.total_collections,
        }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _list_state_paths(cwd: Path | None = None) -> list[Path]:
    root = collector_daemon_dir(cwd)
    if not root.exists():
        return []
    return sorted(root.glob("*.state.json"))


def _state_priority(state: dict[str, object]) -> int:
    try:
        return int(state.get("priority", 50))
    except Exception:
        return 50


def read_state(path: Path | None = None) -> dict[str, object] | None:
    state_path = path or collector_daemon_state_path(instance="default")
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8"))


def is_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but current user cannot signal it.
        return True


def start_daemon(
    *,
    instance: str,
    source: str,
    interval_sec: int,
    priority: int = 50,
    tags: list[str] | None = None,
    max_collections_per_minute: int | None = None,
    max_running: int | None = None,
    preempt_lower_priority: bool = False,
    dsn: str | None = None,
    cwd: Path | None = None,
) -> CollectorDaemonState:
    if interval_sec <= 0:
        raise ValueError("interval_sec must be > 0")
    if priority < 1 or priority > 100:
        raise ValueError("priority must be in range [1, 100]")
    if max_collections_per_minute is not None and max_collections_per_minute <= 0:
        raise ValueError("max_collections_per_minute must be > 0")
    if max_running is not None and max_running <= 0:
        raise ValueError("max_running must be > 0")

    state_path = collector_daemon_state_path(cwd, instance=instance)
    log_path = collector_daemon_log_path(cwd, instance=instance)
    existing = read_state(state_path)
    if existing and is_pid_running(int(existing["pid"])):
        raise RuntimeError(f"collector daemon already running with pid={existing['pid']}")

    running_daemons = [item for item in list_daemons(cwd=cwd, include_stale=False) if item.get("running")]
    if max_running is not None and len(running_daemons) >= max_running:
        if not preempt_lower_priority:
            raise RuntimeError(
                f"max_running limit reached ({max_running}); use --preempt-lower-priority to allow replacement."
            )
        target = min(running_daemons, key=_state_priority)
        target_priority = _state_priority(target)
        if target_priority >= priority:
            raise RuntimeError(
                "max_running reached and no lower-priority daemon is eligible for preemption."
            )
        target_instance = str(target.get("instance", "default"))
        stop_result = stop_daemon(instance=target_instance, cwd=cwd)
        if not stop_result.get("stopped"):
            raise RuntimeError(f"failed to preempt instance={target_instance}: {stop_result}")

    cmd = [
        sys.executable,
        "-m",
        "dbk.cli",
        "collect",
        "daemon",
        "run",
        "--instance",
        instance,
        "--source",
        source,
        "--interval-sec",
        str(interval_sec),
    ]
    if dsn:
        cmd.extend(["--dsn", dsn])
    if max_collections_per_minute is not None:
        cmd.extend(["--max-collections-per-minute", str(max_collections_per_minute)])
    cmd.extend(["--state-path", str(state_path)])

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as logf:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=str(cwd or Path.cwd()),
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )

    normalized_tags = sorted({item.strip() for item in (tags or []) if item.strip()})
    state = CollectorDaemonState(
        pid=proc.pid,
        instance=instance,
        source=source,
        interval_sec=interval_sec,
        priority=priority,
        tags=normalized_tags,
        max_collections_per_minute=max_collections_per_minute,
        started_at=utc_now_iso(),
        log_path=str(log_path),
    )
    _write_json(state_path, state.to_dict())
    return state


def stop_daemon(
    *,
    instance: str = "default",
    cwd: Path | None = None,
    timeout_sec: float = 5.0,
) -> dict[str, object]:
    state_path = collector_daemon_state_path(cwd, instance=instance)
    state = read_state(state_path)
    if state is None:
        return {"stopped": False, "reason": "not_running", "instance": instance}

    pid = int(state["pid"])
    if not is_pid_running(pid):
        state_path.unlink(missing_ok=True)
        return {"stopped": True, "pid": pid, "signal": "none", "instance": state.get("instance", instance)}

    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        return {
            "stopped": False,
            "pid": pid,
            "reason": "permission_denied_on_sigterm",
            "instance": state.get("instance", instance),
        }
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not is_pid_running(pid):
            state_path.unlink(missing_ok=True)
            return {"stopped": True, "pid": pid, "signal": "SIGTERM", "instance": state.get("instance", instance)}
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except PermissionError:
        return {
            "stopped": False,
            "pid": pid,
            "reason": "permission_denied_on_sigkill",
            "instance": state.get("instance", instance),
        }
    state_path.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid, "signal": "SIGKILL", "instance": state.get("instance", instance)}


def list_daemons(
    *,
    cwd: Path | None = None,
    include_stale: bool = True,
    tag: str | None = None,
    source: str | None = None,
    instance_pattern: str | None = None,
    min_priority: int | None = None,
) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for path in _list_state_paths(cwd):
        state = read_state(path)
        if not state:
            continue
        running = is_pid_running(int(state["pid"]))
        payload = dict(state)
        payload["running"] = running
        payload["state_path"] = str(path)
        payload_tags = payload.get("tags")
        if not isinstance(payload_tags, list):
            payload["tags"] = []
        if tag:
            state_tags = [str(item) for item in payload["tags"]]
            if tag not in state_tags:
                continue
        if source and str(payload.get("source", "")) != source:
            continue
        if instance_pattern:
            instance_name = str(payload.get("instance", ""))
            if not fnmatch(instance_name, instance_pattern):
                continue
        if min_priority is not None and _state_priority(payload) < min_priority:
            continue
        if running or include_stale:
            payloads.append(payload)
    payloads.sort(key=lambda item: (_state_priority(item), str(item.get("instance", ""))), reverse=True)
    return payloads


def daemon_status(*, instance: str | None = None, cwd: Path | None = None) -> dict[str, object]:
    if instance is None:
        daemons = list_daemons(cwd=cwd, include_stale=True)
        return {"running": any(bool(item.get("running")) for item in daemons), "daemons": daemons}

    state_path = collector_daemon_state_path(cwd, instance=instance)
    state = read_state(state_path)
    if state is None:
        return {"running": False, "instance": instance}
    pid = int(state["pid"])
    running = is_pid_running(pid)
    payload = dict(state)
    payload["running"] = running
    payload["state_path"] = str(state_path)
    return payload


def stop_all_daemons(*, cwd: Path | None = None, timeout_sec: float = 5.0) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for state_path in _list_state_paths(cwd):
        state = read_state(state_path)
        if not state:
            continue
        instance = str(state.get("instance", "default"))
        results.append(stop_daemon(instance=instance, cwd=cwd, timeout_sec=timeout_sec))
    all_stopped = all(bool(item.get("stopped")) for item in results) if results else True
    return {"stopped": all_stopped, "results": results}


def run_loop(
    *,
    collect_once: Callable[[], tuple[int, list[str]]],
    state_path: Path | None = None,
    interval_sec: int = 15,
    max_collections_per_minute: int | None = None,
) -> int:
    target_state_path = state_path or collector_daemon_state_path(instance="default")
    target_state = read_state(target_state_path) or {}
    state_max_cpm_raw = target_state.get("max_collections_per_minute", max_collections_per_minute)
    state_max_cpm = int(state_max_cpm_raw) if state_max_cpm_raw is not None else None
    state = CollectorDaemonState(
        pid=os.getpid(),
        instance=str(target_state.get("instance", "unknown")),
        source=str(target_state.get("source", "unknown")),
        interval_sec=int(target_state.get("interval_sec", interval_sec)),
        priority=int(target_state.get("priority", 50)),
        tags=[str(item) for item in target_state.get("tags", [])] if isinstance(target_state.get("tags"), list) else [],
        max_collections_per_minute=state_max_cpm,
        started_at=str(target_state.get("started_at", utc_now_iso())),
        log_path=str(target_state.get("log_path", collector_daemon_log_path(instance="default"))),
        last_heartbeat_at=target_state.get("last_heartbeat_at"),  # type: ignore[arg-type]
        last_success_at=target_state.get("last_success_at"),  # type: ignore[arg-type]
        last_error=target_state.get("last_error"),  # type: ignore[arg-type]
        total_collections=int(target_state.get("total_collections", 0)),
    )

    stop_flag = {"stop": False}

    def _handle_signal(_sig: int, _frame: object) -> None:
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    recent_collection_times: list[datetime] = []

    while not stop_flag["stop"]:
        now = utc_now_iso()
        state.last_heartbeat_at = now
        try:
            if state.max_collections_per_minute is not None:
                threshold = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
                recent_collection_times = [item for item in recent_collection_times if item >= threshold]
                if len(recent_collection_times) >= state.max_collections_per_minute:
                    state.last_error = "throttled_by_max_collections_per_minute"
                    _write_json(target_state_path, state.to_dict())
                    time.sleep(1)
                    continue
            collected, warnings = collect_once()
            state.total_collections += collected
            state.last_success_at = now
            state.last_error = "; ".join(warnings) if warnings else None
            recent_collection_times.append(datetime.now(tz=timezone.utc))
        except Exception as exc:  # pragma: no cover - runtime safeguard
            state.last_error = str(exc)
        _write_json(target_state_path, state.to_dict())
        for _ in range(state.interval_sec):
            if stop_flag["stop"]:
                break
            time.sleep(1)
    return 0
