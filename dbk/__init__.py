"""DBK package."""

__all__ = [
    "__version__",
    "get_plugin_registry",
    "load_plugins",
    "PluginABC",
    "PluginRegistry",
    "hookimpl",
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

