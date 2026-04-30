from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Environment variable overrides.
# DBK stores config under ~/.dbk/ but individual settings can be overridden via env vars.
ENV_OVERRIDES = {
    "DBK_RUNTIME_DB_PATH": "DBK_RUNTIME_DB_PATH",
    "DBK_ARTIFACTS_ROOT": "DBK_ARTIFACTS_ROOT",
    "DBK_PG_DSN": "DBK_PG_DSN",
    "DBK_COLLECT_INTERVAL_SEC": "DBK_COLLECT_INTERVAL_SEC",
    "DBK_CLEANUP_INTERVAL_SEC": "DBK_CLEANUP_INTERVAL_SEC",
}


def workspace_root(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def dbk_root(cwd: Path | None = None) -> Path:
    # Allow override via env var for testing and custom deployments.
    if os.environ.get("DBK_ROOT"):
        return Path(os.environ["DBK_ROOT"]).resolve()
    return workspace_root(cwd) / ".dbk"


def runtime_db_path(cwd: Path | None = None) -> Path:
    if os.environ.get("DBK_RUNTIME_DB_PATH"):
        return Path(os.environ["DBK_RUNTIME_DB_PATH"]).resolve()
    return dbk_root(cwd) / "runtime.sqlite"


def artifacts_root(cwd: Path | None = None) -> Path:
    if os.environ.get("DBK_ARTIFACTS_ROOT"):
        return Path(os.environ["DBK_ARTIFACTS_ROOT"]).resolve()
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


# ----------------------------------------------------------------------
# Config validation.
# ----------------------------------------------------------------------

@dataclass
class ConfigProblem:
    field: str
    message: str


@dataclass
class ValidatedConfig:
    """Result of validating DBK configuration."""
    ok: bool
    problems: list[ConfigProblem]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "problems": [{"field": p.field, "message": p.message} for p in self.problems],
        }


def validate_config() -> ValidatedConfig:
    """Validate the current DBK configuration.

    Checks:
    - Workspace directories are writable
    - No conflicting env var settings
    - Interval values are in sane ranges
    """
    problems: list[ConfigProblem] = []

    # Check workspace is writable.
    try:
        root = dbk_root()
        root.mkdir(parents=True, exist_ok=True)
        test_file = root / ".write-test"
        test_file.write_text("test")
        test_file.unlink()
    except PermissionError:
        problems.append(ConfigProblem(field="dbk_root", message=f"dbk_root={root} is not writable"))
    except OSError as exc:
        problems.append(ConfigProblem(field="dbk_root", message=f"dbk_root={root} cannot be created: {exc}"))

    # Check DB path directory is writable.
    try:
        db_path = runtime_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        problems.append(ConfigProblem(field="runtime_db_path", message=f"DB directory {db_path.parent} is not writable"))
    except OSError as exc:
        problems.append(ConfigProblem(field="runtime_db_path", message=str(exc)))

    # Check artifacts root is writable.
    try:
        art_root = artifacts_root()
        art_root.mkdir(parents=True, exist_ok=True)
        test_file = art_root / ".write-test"
        test_file.write_text("test")
        test_file.unlink()
    except PermissionError:
        problems.append(ConfigProblem(field="artifacts_root", message=f"artifacts_root={art_root} is not writable"))
    except OSError as exc:
        problems.append(ConfigProblem(field="artifacts_root", message=str(exc)))

    # Validate interval env vars are positive integers.
    for env_var in ("DBK_COLLECT_INTERVAL_SEC", "DBK_CLEANUP_INTERVAL_SEC"):
        raw = os.environ.get(env_var)
        if raw is not None:
            try:
                val = int(raw)
                if val <= 0:
                    problems.append(ConfigProblem(field=env_var, message=f"{env_var} must be positive, got {val}"))
            except ValueError:
                problems.append(ConfigProblem(field=env_var, message=f"{env_var} must be an integer, got {raw!r}"))

    return ValidatedConfig(ok=len(problems) == 0, problems=problems)
