from __future__ import annotations

import re
from pathlib import Path


def workspace_root(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def dbk_root(cwd: Path | None = None) -> Path:
    return workspace_root(cwd) / ".dbk"


def runtime_db_path(cwd: Path | None = None) -> Path:
    return dbk_root(cwd) / "runtime.sqlite"


def artifacts_root(cwd: Path | None = None) -> Path:
    return dbk_root(cwd) / "artifacts" / "runtime"


def _instance_file_key(instance: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", instance.strip())
    return key or "default"


def collector_daemon_dir(cwd: Path | None = None) -> Path:
    return dbk_root(cwd) / "collector-daemons"


def collector_daemon_state_path(cwd: Path | None = None, instance: str = "default") -> Path:
    return collector_daemon_dir(cwd) / f"{_instance_file_key(instance)}.state.json"


def collector_daemon_log_path(cwd: Path | None = None, instance: str = "default") -> Path:
    return collector_daemon_dir(cwd) / f"{_instance_file_key(instance)}.log"


def runtime_cleanup_daemon_state_path(cwd: Path | None = None) -> Path:
    return dbk_root(cwd) / "runtime-cleanup-daemon-state.json"


def runtime_cleanup_daemon_log_path(cwd: Path | None = None) -> Path:
    return dbk_root(cwd) / "runtime-cleanup-daemon.log"


def runtime_cleanup_history_path(cwd: Path | None = None) -> Path:
    return dbk_root(cwd) / "runtime-cleanup-history.jsonl"
