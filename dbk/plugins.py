"""Plugin system for DBK Agent.

Plugins can register tools, modify system prompts, add API routes, and more.
Plugins are discovered via entry points under `dbk.plugins` and loaded at startup.

Directory-based plugins are supported by dropping a Python package under:
    ~/.dbk/plugins/   (or DBK_PLUGIN_DIR env var)

A minimal plugin looks like:

    # my_plugin/plugin.py
    from dbk.plugins import hookimpl

    @hookimpl
    def dbk_tool_register(registry):
        registry.register(MyTool(...))

    @hookimpl
    def dbk_system_prompt(parts):
        parts.append("[Custom instruction from my_plugin]")

Or via the PluginABC for richer plugins:

    from dbk.plugins import PluginABC, hookimpl

    class MyPlugin(PluginABC):
        name = "my_plugin"
        version = "1.0.0"

        def dbk_tool_register(self, registry):
            registry.register(MyTool(...))
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dbk.agent.tools import ToolRegistry

logger = logging.getLogger(__name__)

# ── Hook specification ────────────────────────────────────────────────────────

_HOOK_NAMES = frozenset([
    "dbk_tool_register",
    "dbk_system_prompt",
    "dbk_agent_init",
    "dbk_post_message",
    "dbk_api_routes",
    "dbk_cleanup",
])


def hookimpl(func):
    """Decorator: mark a function as a plugin hook implementation.

    The decorated function must match one of the known hook names
    listed in `_HOOK_NAMES`.  Example::

        @hookimpl
        def dbk_tool_register(registry):
            registry.register(MyTool(...))
    """
    func._dbk_hook = True  # type: ignore[attr-defined]
    return func


# ── Abstract plugin base ─────────────────────────────────────────────────────


class PluginABC(ABC):
    """Base class for structured DBK plugins.

    Subclass this and override the hook methods you want to implement.
    The `name` attribute is required and must be unique across loaded plugins.

    Attributes
    ----------
    name : str
        Unique identifier for this plugin.
    version : str
        Plugin version string (shown in --info output).
    enabled : bool
        Set to False to disable the plugin without removing it from the path.
    """

    name: str = "base"
    version: str = "0.0.0"
    enabled: bool = True

    @abstractmethod
    def dbk_tool_register(self, registry: "ToolRegistry") -> None:
        """Register tools with the agent's ToolRegistry.

        Parameters
        ----------
        registry : ToolRegistry
            The shared tool registry. Call `registry.register(tool)` for each tool.
        """
        raise NotImplementedError

    def dbk_system_prompt(self, parts: list[str]) -> None:
        """Append text fragments to the agent's system prompt.

        Parameters
        ----------
        parts : list[str]
            List of prompt fragments. Append to this list.
        """
        pass

    def dbk_agent_init(self, agent: Any) -> None:
        """Called once after the Agent is constructed.

        Parameters
        ----------
        agent : Agent
            The constructed Agent instance.
        """
        pass

    def dbk_post_message(self, message: str, result: dict[str, Any]) -> None:
        """Called after every user message is processed.

        Parameters
        ----------
        message : str
            The original user message.
        result : dict[str, Any]
            The result dict returned by `agent.process_message()`.
        """
        pass

    def dbk_api_routes(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Return additional FastAPI routes to register.

        Returns
        -------
        list[tuple[str, str, dict[str, Any]]]
            Each entry is ``(path, method, route_kwargs)`` where ``route_kwargs``
            must contain at least a ``handler`` callable that FastAPI can mount.
            Example: ``("/my-plugin/status", "GET", {"handler": my_handler})``
        """
        return []

    def dbk_cleanup(self) -> None:
        """Called when the agent/shell is shutting down.  Use for resource teardown."""
        pass


# ── Plugin registry ──────────────────────────────────────────────────────────


class PluginRegistry:
    """Discovers, loads, and coordinates all DBK plugins."""

    def __init__(self) -> None:
        self._plugins: list[PluginABC] = []
        self._hook_funcs: dict[str, list[Any]] = {name: [] for name in _HOOK_NAMES}
        self._loaded = False

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _plugin_dirs(self) -> list[Path]:
        """Return plugin search directories in priority order."""
        dirs: list[Path] = []
        if env_dir := os.environ.get("DBK_PLUGIN_DIR"):
            dirs.append(Path(env_dir).resolve())
        dirs.append(self._default_plugin_dir())
        return dirs

    def _default_plugin_dir(self) -> Path:
        """~/.dbk/plugins/"""
        home = Path.home()
        return home / ".dbk" / "plugins"

    def discover(self) -> list[str]:
        """Discover and load all available plugins.

        Scans:
        1. ``dbk.plugins`` entry points (installed packages)
        2. ``DBK_PLUGIN_DIR`` if set
        3. ``~/.dbk/plugins/``

        Returns a list of loaded plugin names.
        """
        loaded: list[str] = []

        # Entry points
        loaded += self._load_entry_point_plugins()

        # Directory plugins
        for plugin_dir in self._plugin_dirs():
            loaded += self._load_dir_plugins(plugin_dir)

        self._loaded = True
        return loaded

    def _load_entry_point_plugins(self) -> list[str]:
        loaded: list[str] = []
        try:
            from importlib.metadata import entry_points
        except ImportError:
            return loaded

        try:
            eps = entry_points(group="dbk.plugins")
        except TypeError:
            # Python < 3.10: entry_points() returns a dict
            all_eps = entry_points()
            eps = all_eps.get("dbk.plugins", [])

        for ep in eps:
            try:
                mod = importlib.import_module(ep.module)
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if isinstance(obj, type) and issubclass(obj, PluginABC) and obj is not PluginABC:
                        self.register(obj())
                        loaded.append(obj.name)
                        logger.debug("Loaded plugin from entry point: %s", obj.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load plugin from entry point %s: %s", ep, exc)
        return loaded

    def _load_dir_plugins(self, plugin_dir: Path) -> list[str]:
        loaded: list[str] = []
        if not plugin_dir.is_dir():
            return loaded

        for item in sorted(plugin_dir.iterdir()):
            # Skip non-packages and private dirs
            if item.name.startswith("_") or item.name.startswith("."):
                continue
            if item.is_dir():
                # Expect <name>/plugin.py or <name>/__init__.py
                init = item / "__init__.py"
                plugin_py = item / "plugin.py"
                if not (init.exists() or plugin_py.exists()):
                    continue
                mod_path = plugin_py if plugin_py.exists() else init
                plugin_name = item.name
            elif item.suffix == ".py" and not item.name.startswith("_"):
                mod_path = item
                plugin_name = item.stem
            else:
                continue

            try:
                spec = importlib.util.spec_from_file_location(f"dbk_plugin_{plugin_name}", mod_path)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = mod
                    spec.loader.exec_module(mod)
                else:
                    continue

                # Collect PluginABC subclasses from the module
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if isinstance(obj, type) and issubclass(obj, PluginABC) and obj is not PluginABC:
                        instance = obj()
                        self.register(instance)
                        loaded.append(instance.name)
                        logger.debug("Loaded directory plugin: %s", instance.name)

                # Also collect plain @hookimpl functions
                self._collect_hook_funcs(mod, plugin_name)
                logger.debug("Loaded plugin dir: %s", plugin_name)
                loaded.append(plugin_name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load plugin from %s: %s", mod_path, exc)
        return loaded

    def _collect_hook_funcs(self, mod: Any, plugin_name: str) -> None:
        """Collect standalone @hookimpl-decorated functions from a module."""
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name, None)
            if callable(obj) and getattr(obj, "_dbk_hook", False):
                for hook_name in _HOOK_NAMES:
                    if attr_name == hook_name:
                        self._hook_funcs[hook_name].append(obj)
                        logger.debug("Registered hook %s from plugin %s", hook_name, plugin_name)

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, plugin: PluginABC) -> None:
        """Register a plugin instance."""
        if not plugin.enabled:
            return
        if any(p.name == plugin.name for p in self._plugins):
            logger.warning("Plugin already registered: %s", plugin.name)
            return
        self._plugins.append(plugin)
        # Also collect @hookimpl methods
        for name in _HOOK_NAMES:
            method = getattr(plugin, name, None)
            if callable(method):
                self._hook_funcs[name].append(method)
        logger.info("Registered DBK plugin: %s v%s", plugin.name, plugin.version)

    def unregister(self, name: str) -> bool:
        """Remove a plugin by name. Returns True if it was found."""
        for i, p in enumerate(self._plugins):
            if p.name == name:
                self._plugins.pop(i)
                # Remove its hook funcs too
                for hook_name in _HOOK_NAMES:
                    self._hook_funcs[hook_name] = [
                        f for f in self._hook_funcs[hook_name]
                        if not (hasattr(f, "__self__") and getattr(f, "__self__", None) == p)
                    ]
                return True
        return False

    # ── Hook dispatch ────────────────────────────────────────────────────────

    def _dispatch(self, hook_name: str, default: Any = None, **kwargs: Any) -> Any:
        """Call all implementations of a hook in registration order.

        The first non-None return value is returned (or the last one if all
        return None).  Pass ``default=[]`` when accumulating results.
        """
        results: list[Any] = []
        for impl in self._hook_funcs.get(hook_name, []):
            try:
                result = impl(**kwargs)
                if result is not None:
                    results.append(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Hook %s raised: %s", hook_name, exc)
        if results:
            return results[0] if len(results) == 1 else results
        return default

    def apply_tool_hooks(self, registry: "ToolRegistry") -> None:
        """Call dbk_tool_register on all plugins."""
        for impl in self._hook_funcs.get("dbk_tool_register", []):
            try:
                impl(registry=registry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dbk_tool_register hook failed: %s", exc)

    def build_system_prompt(self, base: str) -> str:
        """Build the final system prompt by letting plugins append to it."""
        parts = [base]
        self._dispatch("dbk_system_prompt", parts=parts)
        return "\n".join(parts)

    def apply_agent_init_hooks(self, agent: Any) -> None:
        """Call dbk_agent_init on all plugins."""
        self._dispatch("dbk_agent_init", agent=agent)

    def apply_post_message_hooks(self, message: str, result: dict[str, Any]) -> None:
        """Call dbk_post_message on all plugins."""
        for impl in self._hook_funcs.get("dbk_post_message", []):
            try:
                impl(message=message, result=result)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dbk_post_message hook failed: %s", exc)

    def apply_cleanup_hooks(self) -> None:
        """Call dbk_cleanup on all plugins."""
        for impl in self._hook_funcs.get("dbk_cleanup", []):
            try:
                impl()
            except Exception as exc:  # noqa: BLE001
                logger.warning("dbk_cleanup hook failed: %s", exc)

    def get_api_routes(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Collect all plugin API routes."""
        routes: list[tuple[str, str, dict[str, Any]]] = []
        for impl in self._hook_funcs.get("dbk_api_routes", []):
            try:
                routes.extend(impl() or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("dbk_api_routes hook failed: %s", exc)
        return routes

    # ── Introspection ────────────────────────────────────────────────────────

    def list_plugins(self) -> list[dict[str, Any]]:
        """Return info about all loaded plugins."""
        return [
            {
                "name": p.name,
                "version": p.version,
                "enabled": p.enabled,
                "hooks": [h for h in _HOOK_NAMES if hasattr(p, h) and callable(getattr(p, h))],
            }
            for p in self._plugins
        ]

    @property
    def plugin_count(self) -> int:
        return len(self._plugins)


# ── Global singleton ─────────────────────────────────────────────────────────


_global_registry: PluginRegistry | None = None


def get_plugin_registry() -> PluginRegistry:
    """Get the global plugin registry (singleton)."""
    global _global_registry
    if _global_registry is None:
        _global_registry = PluginRegistry()
    return _global_registry


def load_plugins() -> PluginRegistry:
    """Discover and register all plugins, returning the global registry."""
    reg = get_plugin_registry()
    reg.discover()
    return reg
