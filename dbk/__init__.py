"""DBK package — Database Kernel observability AI Agent.

Quick start::

    from dbk import DBK          # high-level unified client
    from dbk import DBKClient    # same as DBK, explicit name
    from dbk.sdk_config import SDKConfig  # configuration

    # One-liner
    dbk = DBK()
    dbk.collect()

    # From a PostgreSQL DSN
    client = DBKClient.from_dsn("postgresql://user:***@localhost:5432/mydb")

    # Advanced / per-call config
    cfg = SDKConfig.from_dict({"provider": "mock", "model": "mock"})
    client = DBKClient(cfg={"provider": "anthropic", "model": "claude-3-opus"})
"""
from __future__ import annotations

__all__ = [
    "__version__",
    "get_plugin_registry",
    "load_plugins",
    "PluginABC",
    "PluginRegistry",
    "hookimpl",
    # SDK surface
    "DBK",
    "DBKClient",
    "DBKAsyncClient",
    "RemoteDBKClient",
    "get_default_client",
    "SDKConfig",
    # Exception hierarchy
    "DBKError",
    "DBKConfigError",
    "DBKConnectionError",
    "DBKTimeoutError",
    "DBKValidationError",
    "DBKNotFoundError",
    "DBKWorkflowError",
]
__version__ = "0.1.0"

# Lazy imports to avoid hard dependency on plugin system.
_plugin_reg = None


def get_plugin_registry():
    """Return the global plugin registry (loads it lazily)."""
    global _plugin_reg
    if _plugin_reg is None:
        from dbk.plugins import PluginRegistry
        _plugin_reg = PluginRegistry()
    return _plugin_reg


def load_plugins():
    """Discover and load all DBK plugins."""
    from dbk.plugins import load_plugins as _lp
    return _lp()


# ---------------------------------------------------------------------------
# SDK re-exports (lazy to avoid circular imports).
# ---------------------------------------------------------------------------

def __getattr__(name: str):
    if name == "DBK":
        from dbk.sdk import DBKClient
        return DBKClient
    if name == "DBKClient":
        from dbk.sdk import DBKClient as _DBKClient
        return _DBKClient
    if name == "DBKAsyncClient":
        from dbk.sdk import DBKAsyncClient as _c
        return _c
    if name == "RemoteDBKClient":
        from dbk.sdk import RemoteDBKClient as _r
        return _r
    if name == "get_default_client":
        from dbk.sdk import get_default_client as _gdc
        return _gdc
    if name == "SDKConfig":
        from dbk.sdk_config import SDKConfig as _SDKConfig
        return _SDKConfig
    if name == "DBKError":
        from dbk.sdk import DBKError as _e
        return _e
    if name == "DBKConfigError":
        from dbk.sdk import DBKConfigError as _e
        return _e
    if name == "DBKConnectionError":
        from dbk.sdk import DBKConnectionError as _e
        return _e
    if name == "DBKTimeoutError":
        from dbk.sdk import DBKTimeoutError as _e
        return _e
    if name == "DBKValidationError":
        from dbk.sdk import DBKValidationError as _e
        return _e
    if name == "DBKNotFoundError":
        from dbk.sdk import DBKNotFoundError as _e
        return _e
    if name == "DBKWorkflowError":
        from dbk.sdk import DBKWorkflowError as _e
        return _e
    if name == "PluginABC":
        from dbk.plugins import PluginABC as _p
        return _p
    if name == "PluginRegistry":
        from dbk.plugins import PluginRegistry as _pr
        return _pr
    if name == "hookimpl":
        from dbk.plugins import hookimpl as _h
        return _h
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

