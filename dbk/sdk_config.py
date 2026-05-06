"""SDK configuration dataclasses for DBKClient."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dbk.config_loader import TOMLConfig, TOMLError

# Import TOML parser: tomllib (3.11+) with tomli fallback (3.10).
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


@dataclass
class SDKConfig:
    """Configuration for DBKClient.

    Attributes
    ----------
    provider : str
        LLM provider name (e.g., "mock", "anthropic", "openai").
    model : str
        Model identifier for the provider.
    dbk_root : Path
        Root directory for DBK runtime data.
    pg_dsn : str | None
        PostgreSQL DSN for pgstat collectors.
    log_level : str
        Logging level (DEBUG, INFO, WARNING, ERROR).
    config_path : Path | None
        Path to the config.toml that was loaded (if any).
    """

    provider: str = "mock"
    model: str = "mock"
    dbk_root: Path = field(default_factory=lambda: Path.home() / ".dbk")
    pg_dsn: str | None = None
    log_level: str = "WARNING"
    config_path: Path | None = None
    base_url: str | None = None  # HTTP endpoint for remote/client mode.

    # Internal storage for extra keys loaded from TOML.
    _extra: dict[str, object] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SDKConfig:
        """Build an SDKConfig from a plain dict.

        Accepts keys: provider, model, dbk_root, pg_dsn, log_level, base_url.
        All other keys are stored in _extra and available via extra().
        """
        provider = str(data.get("provider", "mock"))
        model = str(data.get("model", "mock"))
        dbk_root = cls._resolve_path(data.get("dbk_root"))
        pg_dsn = str(data["pg_dsn"]) if data.get("pg_dsn") is not None else None
        log_level = str(data.get("log_level", "WARNING"))
        base_url = str(data["base_url"]) if data.get("base_url") is not None else None

        cfg = cls(
            provider=provider,
            model=model,
            dbk_root=dbk_root,
            pg_dsn=pg_dsn,
            log_level=log_level,
            base_url=base_url,
        )
        # Capture any extra keys not explicitly listed above.
        known = {"provider", "model", "dbk_root", "pg_dsn", "log_level", "base_url"}
        cfg._extra = {k: v for k, v in data.items() if k not in known}
        return cfg

    @classmethod
    def from_toml(cls, path: Path | str | None = None) -> SDKConfig:
        """Load SDKConfig from a config.toml file.

        If path is None, falls back to ~/.dbk/config.toml.
        Raises SDKValidationError if the file exists but cannot be parsed.
        """
        from dbk.sdk_config import SDKValidationError

        if path is None:
            path = Path.home() / ".dbk" / "config.toml"
        path = Path(path)

        if not path.exists():
            # Return defaults if no config file present.
            return cls(config_path=path)

        if tomllib is None:
            raise SDKValidationError(
                "tomli is required to parse TOML on Python < 3.11. "
                "Install it with: pip install tomli"
            )

        try:
            raw = path.read_text(encoding="utf-8")
            data = tomllib.loads(raw)
        except Exception as exc:
            raise SDKValidationError(f"Failed to parse {path}: {exc}") from exc

        # Flatten the [dbk] section if present; otherwise use the top-level.
        if "dbk" in data and isinstance(data["dbk"], dict):
            flat: dict[str, object] = dict(data["dbk"])
        else:
            flat = dict(data)

        # Env var overrides (DBK_PG_DSN, DBK_ROOT, DBK_LOG_LEVEL, etc.)
        if os.environ.get("DBK_PG_DSN"):
            flat["pg_dsn"] = os.environ["DBK_PG_DSN"]
        if os.environ.get("DBK_ROOT"):
            flat["dbk_root"] = os.environ["DBK_ROOT"]
        if os.environ.get("DBK_LOG_LEVEL"):
            flat["log_level"] = os.environ["DBK_LOG_LEVEL"]
        if os.environ.get("DBK_PROVIDER"):
            flat["provider"] = os.environ["DBK_PROVIDER"]
        if os.environ.get("DBK_MODEL"):
            flat["model"] = os.environ["DBK_MODEL"]

        cfg = cls.from_dict(flat)
        cfg.config_path = path
        return cfg

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Validate the config and return a list of error messages.

        Returns an empty list if the config is valid.
        """
        errors: list[str] = []

        valid_providers = {"mock", "anthropic", "openai"}
        if self.provider not in valid_providers:
            errors.append(
                f"Invalid provider '{self.provider}'. "
                f"Expected one of: {', '.join(sorted(valid_providers))}"
            )

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_levels:
            errors.append(
                f"Invalid log_level '{self.log_level}'. "
                f"Expected one of: {', '.join(sorted(valid_levels))}"
            )

        if self.dbk_root is None:
            errors.append("dbk_root must not be None")
        else:
            try:
                self.dbk_root = Path(self.dbk_root)
            except (ValueError, TypeError) as exc:
                errors.append(f"dbk_root is not a valid path: {exc}")

        return errors

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_path(value: object | None) -> Path:
        if value is None:
            return Path.home() / ".dbk"
        return Path(str(value)).expanduser().resolve()

    def extra(self, key: str, default: object = None) -> object:
        """Return an extra key loaded from TOML that is not a named field."""
        return self._extra.get(key, default)

    def apply_env_overrides(self) -> None:
        """Apply environment variable overrides in-place."""
        if os.environ.get("DBK_PG_DSN"):
            self.pg_dsn = os.environ["DBK_PG_DSN"]
        if os.environ.get("DBK_ROOT"):
            self.dbk_root = Path(os.environ["DBK_ROOT"]).expanduser().resolve()
        if os.environ.get("DBK_LOG_LEVEL"):
            self.log_level = os.environ["DBK_LOG_LEVEL"]
        if os.environ.get("DBK_PROVIDER"):
            self.provider = os.environ["DBK_PROVIDER"]
        if os.environ.get("DBK_MODEL"):
            self.model = os.environ["DBK_MODEL"]
        if os.environ.get("DBK_BASE_URL"):
            self.base_url = os.environ["DBK_BASE_URL"]

    def as_dict(self) -> dict[str, object]:
        """Return a plain dict representation of the config."""
        return {
            "provider": self.provider,
            "model": self.model,
            "dbk_root": str(self.dbk_root),
            "pg_dsn": self.pg_dsn,
            "log_level": self.log_level,
            "config_path": str(self.config_path) if self.config_path else None,
            "base_url": self.base_url,
        }


class SDKValidationError(Exception):
    """Raised when SDKConfig validation fails."""

    pass
