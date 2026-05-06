"""TOML configuration loader for DBK.

Loads configuration from:
  1. XDG_CONFIG_HOME/dbk/config.toml  (XDG standard, user-level)
  2. ./config.toml                     (project-level override)
  3. Built-in defaults                 (lowest priority)

Env vars always take precedence over TOML values.

Supports Python 3.10 via tomli, and Python 3.11+ via tomllib.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

# Import TOML parser: tomllib (3.11+) with tomli fallback (3.10).
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "dbk"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"
PROJECT_CONFIG_PATH = Path.cwd() / "config.toml"


class TOMLError(Exception):
    """Raised when loading or parsing config.toml fails."""


# -----------------------------------------------------------------------
# Built-in defaults (merged into every TOMLConfig instance).
# -----------------------------------------------------------------------

_DEFAULTS: dict[str, object] = {
    "dbk": {
        "root": "",
        "runtime_db_path": "",
        "artifacts_root": "",
    },
    "api_server": {
        "host": "127.0.0.1",
        "port": 8080,
        "workers": 1,
        "log_level": "info",
    },
    "alerting": {
        "interval_sec": 60,
        "prometheus_host": "127.0.0.1",
        "prometheus_port": 9090,
        "cooldown_sec": 300,
    },
    "agent": {
        "provider": "mock",
        "model": "",
        "archive_interval": 5,
    },
    "providers": {
        "openai_api_key": "",
        "openai_model": "gpt-4o-mini",
        "openai_base_url": "",
        "anthropic_api_key": "",
        "anthropic_model": "claude-3-5-haiku-20241022",
        "anthropic_base_url": "",
    },
    "logging": {
        "level": "info",
        "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    },
}


def _deep_merge(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    """Recursively merge overlay into base. overlay takes precedence."""
    result: dict[str, object] = dict(base)
    for key, val in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(cast(dict[str, object], result[key]), val)
        else:
            result[key] = val
    return result


class TOMLConfig:
    """Singleton loader for TOML configuration files with XDG + project-level override.

    Config precedence (highest to lowest):
      1. Environment variables (e.g. DBK_PORT=9000)
      2. Project-level config.toml (in cwd)
      3. XDG user config (~/.config/dbk/config.toml)
      4. Built-in defaults

    When created with an explicit config_path (not the default), only that file
    is loaded — no defaults, no XDG, no project-level merging. This preserves
    backward compatibility with tests.

    Env vars always take precedence. Values are lazily loaded on first access.
    """

    _instance: TOMLConfig | None = None

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        config_dir: Path | str | None = None,
    ) -> None:
        if config_dir is not None and config_path is not None:
            raise ValueError("Provide only one of config_dir or config_path, not both")
        if config_path is not None:
            self._config_path = Path(config_path)
        elif config_dir is not None:
            self._config_path = Path(config_dir) / "config.toml"
        else:
            self._config_path = DEFAULT_CONFIG_PATH

        # Track whether this is the default path (for XDG/project-level loading)
        # or an explicit path (for backward-compat tests).
        self._is_default_path: bool = self._config_path == DEFAULT_CONFIG_PATH

        self._data: dict[str, object] | None = None
        self._loaded: bool = False

    @classmethod
    def get_instance(cls) -> TOMLConfig:
        """Get the global TOMLConfig singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (useful for testing)."""
        cls._instance = None

    def _find_config_files(self) -> list[Path]:
        """Return ordered list of config files to load (highest-priority last).

        For explicit config_path (tests): only that file.
        For default path: XDG then project-level (project overrides XDG).
        """
        if not self._is_default_path:
            # Explicit path — load only that file, no defaults.
            if self._config_path.exists():
                return [self._config_path]
            return []

        paths: list[Path] = []
        xdg = DEFAULT_CONFIG_PATH
        if xdg.exists():
            paths.append(xdg)
        proj = PROJECT_CONFIG_PATH
        if proj.exists():
            paths.append(proj)
        return paths

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if tomllib is None:
            raise TOMLError(
                "tomli is required for Python < 3.11. "
                "Install it with: pip install tomli"
            )
        # Start with defaults (only when using the default path).
        if self._is_default_path:
            self._data = _deep_merge({}, _DEFAULTS)
        else:
            self._data = {}

        for cfg_path in self._find_config_files():
            try:
                raw = cfg_path.read_text(encoding="utf-8")
                parsed = tomllib.loads(raw)
                self._data = _deep_merge(self._data, parsed)
            except Exception as exc:
                raise TOMLError(
                    f"Failed to parse {cfg_path}: {exc}"
                ) from exc

        self._loaded = True

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def exists(self) -> bool:
        """Return True if config.toml exists and is readable."""
        if not self._config_path.exists():
            return False
        try:
            self._ensure_loaded()
        except TOMLError:
            return False
        return True

    def get(self, *keys: str, default: object = None) -> object:
        """Get a nested value from the TOML config.

        Supports dot-separated keys (e.g. get("dbk", "provider")) or
        individual key parts (e.g. get("dbk", "provider")).

        Env vars always take precedence. For two-part keys, the env var is
        DBK_SECTION_KEY; for single-part keys, DBK_KEY.
        """
        self._ensure_loaded()
        assert self._data is not None

        # Check env var override.
        if len(keys) >= 2:
            env_var = f"DBK_{keys[0].upper()}_{keys[-1].upper()}"
        elif keys:
            env_var = f"DBK_{keys[0].upper()}"
        else:
            env_var = None
        if env_var and os.environ.get(env_var):
            return os.environ[env_var]

        if not self._data:
            return default

        val: object = self._data
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default

        return val

    def get_str(self, *keys: str, default: str = "") -> str:
        """Get a string value."""
        val = self.get(*keys)
        if val is None:
            return default
        return str(val)

    def get_int(self, *keys: str, default: int = 0) -> int:
        """Get an integer value."""
        val = self.get(*keys)
        if val is None:
            return default
        try:
            return int(float(cast(str, val)))
        except (ValueError, TypeError):
            return default

    def get_bool(self, *keys: str, default: bool = False) -> bool:
        """Get a boolean value."""
        val = self.get(*keys)
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return default

    def as_dict(self) -> dict[str, object]:
        """Return the entire parsed config as a dict."""
        self._ensure_loaded()
        assert self._data is not None
        return dict(self._data)

    def __repr__(self) -> str:
        return f"TOMLConfig(path={self._config_path!r})"
