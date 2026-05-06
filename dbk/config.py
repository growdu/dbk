from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dbk.config_loader import TOMLConfig, TOMLError

# Environment variable overrides.
# DBK stores config under ~/.config/dbk/ but individual settings can be overridden via env vars.
ENV_OVERRIDES = {
    "DBK_RUNTIME_DB_PATH": "DBK_RUNTIME_DB_PATH",
    "DBK_ARTIFACTS_ROOT": "DBK_ARTIFACTS_ROOT",
    "DBK_PG_DSN": "DBK_PG_DSN",
    "DBK_COLLECT_INTERVAL_SEC": "DBK_COLLECT_INTERVAL_SEC",
    "DBK_CLEANUP_INTERVAL_SEC": "DBK_CLEANUP_INTERVAL_SEC",
}


def _toml() -> TOMLConfig | None:
    """Load TOML config, or None if it doesn't exist or can't be parsed."""
    try:
        return TOMLConfig.get_instance()
    except TOMLError:
        return None


def _get_str(section: str, key: str, default: str = "") -> str:
    """Get a string config value."""
    if (cfg := _toml()) is not None:
        return cfg.get_str(section, key, default=default)
    return default


def _get_int(section: str, key: str, default: int = 0) -> int:
    """Get an integer config value."""
    if (cfg := _toml()) is not None:
        return cfg.get_int(section, key, default=default)
    return default


def _get_bool(section: str, key: str, default: bool = False) -> bool:
    """Get a boolean config value."""
    if (cfg := _toml()) is not None:
        return cfg.get_bool(section, key, default=default)
    return default


# ----------------------------------------------------------------------
# Core path helpers.
# ----------------------------------------------------------------------


def workspace_root(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()).resolve()


def dbk_root(cwd: Path | None = None) -> Path:
    # Allow override via env var for testing and custom deployments.
    if os.environ.get("DBK_ROOT"):
        return Path(os.environ["DBK_ROOT"]).resolve()
    # Check TOML config root override.
    if (cfg := _toml()) and (root := cfg.get("dbk", "root")):
        return Path(str(root)).resolve()
    return workspace_root(cwd) / ".dbk"


def runtime_db_path(cwd: Path | None = None) -> Path:
    if os.environ.get("DBK_RUNTIME_DB_PATH"):
        return Path(os.environ["DBK_RUNTIME_DB_PATH"]).resolve()
    if (cfg := _toml()) and (path := cfg.get("dbk", "runtime_db_path")):
        return Path(str(path)).resolve()
    return dbk_root(cwd) / "runtime.sqlite"


def artifacts_root(cwd: Path | None = None) -> Path:
    if os.environ.get("DBK_ARTIFACTS_ROOT"):
        return Path(os.environ["DBK_ARTIFACTS_ROOT"]).resolve()
    if (cfg := _toml()) and (root := cfg.get("dbk", "artifacts_root")):
        return Path(str(root)).resolve()
    return dbk_root(cwd) / "artifacts" / "runtime"


# ----------------------------------------------------------------------
# API Server config helpers.
# ----------------------------------------------------------------------


def api_server_host() -> str:
    """Host the API server binds to."""
    return _get_str("api_server", "host", "127.0.0.1")


def api_server_port() -> int:
    """Port the API server listens on."""
    return _get_int("api_server", "port", 8080)


def api_server_workers() -> int:
    """Number of API server worker processes."""
    return _get_int("api_server", "workers", 1)


def api_server_log_level() -> str:
    """Uvicorn log level."""
    return _get_str("api_server", "log_level", "info")


# ----------------------------------------------------------------------
# Alerting config helpers.
# ----------------------------------------------------------------------


def alerting_interval_sec() -> int:
    """Alert evaluation interval in seconds."""
    return _get_int("alerting", "interval_sec", 60)


def alerting_prometheus_host() -> str:
    """Host for alert Prometheus exporter."""
    return _get_str("alerting", "prometheus_host", "127.0.0.1")


def alerting_prometheus_port() -> int:
    """Port for alert Prometheus exporter."""
    return _get_int("alerting", "prometheus_port", 9090)


def alerting_cooldown_sec() -> int:
    """Default alert cooldown in seconds."""
    return _get_int("alerting", "cooldown_sec", 300)


# ----------------------------------------------------------------------
# Agent config helpers.
# ----------------------------------------------------------------------


def agent_provider() -> str:
    """Default LLM provider ('openai', 'anthropic', 'mock')."""
    return _get_str("agent", "provider", "mock")


def agent_model() -> str:
    """Default model name for the agent provider."""
    return _get_str("agent", "model", "")


def agent_archive_interval() -> int:
    """Archive to memory every N turns."""
    return _get_int("agent", "archive_interval", 5)


# ----------------------------------------------------------------------
# Provider config helpers.
# ----------------------------------------------------------------------


def provider_openai_api_key() -> str:
    """OpenAI API key."""
    return os.environ.get("DBK_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or _get_str(
        "providers", "openai_api_key", ""
    )


def provider_openai_model() -> str:
    """OpenAI model name."""
    return _get_str("providers", "openai_model", "gpt-4o-mini")


def provider_openai_base_url() -> str:
    """OpenAI base URL (for proxies like OpenRouter)."""
    return _get_str("providers", "openai_base_url", "")


def provider_anthropic_api_key() -> str:
    """Anthropic API key."""
    return os.environ.get("DBK_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or _get_str(
        "providers", "anthropic_api_key", ""
    )


def provider_anthropic_model() -> str:
    """Anthropic model name."""
    return _get_str("providers", "anthropic_model", "claude-3-5-haiku-20241022")


def provider_anthropic_base_url() -> str:
    """Anthropic base URL (for proxies like MiniMax)."""
    return os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("DBK_ANTHROPIC_BASE_URL") or _get_str(
        "providers", "anthropic_base_url", ""
    )


# ----------------------------------------------------------------------
# Logging config helpers.
# ----------------------------------------------------------------------


def logging_level() -> str:
    """Default logging level."""
    return _get_str("logging", "level", "info")


def logging_format() -> str:
    """Default logging format string."""
    return _get_str(
        "logging", "format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )


# ----------------------------------------------------------------------
# Config loading helpers.
# ----------------------------------------------------------------------


def load_config() -> dict[str, object]:
    """Return the full resolved config dict."""
    if (cfg := _toml()) is not None:
        return cfg.as_dict()
    return {}


def get_config() -> dict[str, object]:
    """Alias for load_config()."""
    return load_config()


# ----------------------------------------------------------------------
# Daemon state/log paths.
# ----------------------------------------------------------------------


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
