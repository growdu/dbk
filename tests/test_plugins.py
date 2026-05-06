"""Tests for dbk.plugins."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dbk import plugins


# ----------------------------------------------------------------------
#  Test fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def clean_registry():
    """Return a fresh PluginRegistry and clean up any global state."""
    plugins._global_registry = None
    reg = plugins.PluginRegistry()
    yield reg
    # reset singleton
    plugins._global_registry = None


class DummyTool:
    """Minimal stand-in so we can call registry.register without real tools."""


# ----------------------------------------------------------------------
#  hookimpl decorator
# ----------------------------------------------------------------------


class TestHookImpl:
    def test_hookimpl_valid_sets_attr(self):
        """@hookimpl on a known hook name sets _dbk_hook = True."""

        @plugins.hookimpl
        def dbk_tool_register(registry):
            pass

        assert getattr(dbk_tool_register, "_dbk_hook", False) is True

    @pytest.mark.parametrize(
        "invalid_name",
        [
            "dbk_unknown_hook",
            "dbk_tool_register_extra",
            "tool_register",
            "hookimpl",
            "dbk_cleanup_extra",
            "dbk_api",
            "dbk_foo",
        ],
    )
    def test_hookimpl_invalid_name_raises(self, invalid_name):
        """@hookimpl on an unknown name raises ValueError."""
        code = f"""
def {invalid_name}(registry):
    pass
"""
        namespace: dict = {}
        exec(code, namespace)
        func = namespace[invalid_name]
        with pytest.raises(ValueError):
            plugins.hookimpl(func)

    def test_hookimpl_invalid_name_with_suffix(self):
        """@hookimpl on a name that looks similar to a hook name raises ValueError."""
        code = """
def dbk_tool_register_v2(registry):
    pass
"""
        ns: dict = {}
        exec(code, ns)
        func = ns["dbk_tool_register_v2"]
        with pytest.raises(ValueError):
            plugins.hookimpl(func)

    def test_hookimpl_all_known_hooks(self):
        """Every name in _HOOK_NAMES can be used as a hookimpl without error."""
        for name in plugins._HOOK_NAMES:
            code = f"""
def {name}():
    pass
"""
            ns: dict = {}
            exec(code, ns)
            fn = ns[name]
            # Should not raise
            wrapped = plugins.hookimpl(fn)
            assert wrapped._dbk_hook is True

    def test_hookimpl_copies_function(self):
        """@hookimpl returns the function (not a wrapper object)."""

        @plugins.hookimpl
        def dbk_system_prompt(parts):
            pass

        assert callable(dbk_system_prompt)


# ----------------------------------------------------------------------
#  PluginABC
# ----------------------------------------------------------------------


class TestPluginABC:
    def test_default_attributes(self):
        """PluginABC has the documented default attributes."""
        assert plugins.PluginABC.name == "base"
        assert plugins.PluginABC.version == "0.0.0"
        assert plugins.PluginABC.enabled is True

    def test_tool_register_is_abstract(self):
        """Instantiating PluginABC directly raises TypeError (abstract)."""
        with pytest.raises(TypeError, match="abstract"):
            plugins.PluginABC()

    def test_subclass_can_be_instantiated(self):
        """A concrete subclass of PluginABC can be instantiated."""

        class MyPlugin(plugins.PluginABC):
            name = "my_plugin"
            version = "1.2.3"
            enabled = False

            def dbk_tool_register(self, registry):
                pass

        p = MyPlugin()
        assert p.name == "my_plugin"
        assert p.version == "1.2.3"
        assert p.enabled is False

    def test_subclass_without_tool_register_raises_type_error(self):
        """A subclass that doesn't implement dbk_tool_register can't be instantiated (ABC enforcement)."""

        class BrokenPlugin(plugins.PluginABC):
            pass

        with pytest.raises(TypeError, match="abstract"):
            BrokenPlugin()

    def test_non_abstract_hooks_have_default_implementation(self):
        """Non-abstract hooks have pass implementations that return the expected types."""

        class MyPlugin(plugins.PluginABC):
            name = "test"
            version = "0.1.0"

            def dbk_tool_register(self, registry):
                pass

        p = MyPlugin()
        # These should not raise
        p.dbk_system_prompt(parts=[])
        p.dbk_agent_init(agent=None)
        p.dbk_post_message(message="", result={})
        p.dbk_cleanup()
        # dbk_api_routes returns a list
        assert isinstance(p.dbk_api_routes(), list)


# ----------------------------------------------------------------------
#  PluginRegistry._plugin_dirs
# ----------------------------------------------------------------------


class TestPluginDirs:
    def test_respects_dbk_plugin_dir_env_var(self, monkeypatch):
        """_plugin_dirs includes the path in DBK_PLUGIN_DIR."""
        monkeypatch.setenv("DBK_PLUGIN_DIR", "/my/plugins")
        reg = plugins.PluginRegistry()
        dirs = reg._plugin_dirs()
        assert Path("/my/plugins") in dirs

    def test_fallback_to_home_dir(self):
        """_plugin_dirs includes ~/.dbk/plugins/ as fallback."""
        reg = plugins.PluginRegistry()
        dirs = reg._plugin_dirs()
        expected = Path.home() / ".dbk" / "plugins"
        assert expected in dirs

    def test_env_var_before_fallback(self, monkeypatch):
        """DBK_PLUGIN_DIR takes priority over the default directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("DBK_PLUGIN_DIR", tmpdir)
            reg = plugins.PluginRegistry()
            dirs = reg._plugin_dirs()
            # env var dir should come first
            assert dirs[0] == Path(tmpdir)
            assert dirs[1] == Path.home() / ".dbk" / "plugins"


# ----------------------------------------------------------------------
#  PluginRegistry.register / get_registered_plugins
# ----------------------------------------------------------------------


class TestPluginRegistryRegistration:
    def test_register_plugin(self, clean_registry):
        """register() adds a PluginABC instance to the registry."""

        class MyPlugin(plugins.PluginABC):
            name = "reg_test"
            version = "0.0.1"

            def dbk_tool_register(self, registry):
                pass

        clean_registry.register(MyPlugin())
        assert clean_registry.plugin_count == 1

    def test_get_registered_plugins(self, clean_registry):
        """get_registered_plugins() returns the list of registered instances.

        Note: PluginRegistry exposes _plugins directly and list_plugins()
        for introspection; we test that plugins are stored correctly.
        """

        class P1(plugins.PluginABC):
            name = "p1"
            version = "1.0.0"

            def dbk_tool_register(self, registry):
                pass

        class P2(plugins.PluginABC):
            name = "p2"
            version = "2.0.0"

            def dbk_tool_register(self, registry):
                pass

        clean_registry.register(P1())
        clean_registry.register(P2())
        # _plugins is the internal list of registered PluginABC instances
        assert len(clean_registry._plugins) == 2
        assert all(isinstance(p, plugins.PluginABC) for p in clean_registry._plugins)
        # list_plugins() is the public introspection method
        info = clean_registry.list_plugins()
        assert len(info) == 2
        assert [p["name"] for p in info] == ["p1", "p2"]

    def test_disabled_plugin_not_registered(self, clean_registry):
        """register() skips plugins with enabled=False."""

        class DisabledPlugin(plugins.PluginABC):
            name = "disabled"
            version = "0.0.1"
            enabled = False

            def dbk_tool_register(self, registry):
                pass

        clean_registry.register(DisabledPlugin())
        assert clean_registry.plugin_count == 0

    def test_duplicate_plugin_not_registered(self, clean_registry):
        """register() skips a plugin that is already registered by name."""

        class DupPlugin(plugins.PluginABC):
            name = "dup"
            version = "1.0.0"

            def dbk_tool_register(self, registry):
                pass

        clean_registry.register(DupPlugin())
        clean_registry.register(DupPlugin())
        assert clean_registry.plugin_count == 1


# ----------------------------------------------------------------------
#  PluginRegistry.discover
# ----------------------------------------------------------------------


class TestDiscover:
    def test_discover_returns_list_of_names(self, clean_registry):
        """discover() returns a list (may be empty without real plugins)."""
        names = clean_registry.discover()
        assert isinstance(names, list)
        # Should not raise; may be empty

    def test_discover_idempotent(self, clean_registry):
        """Calling discover() twice returns the same names without reloading."""
        # Prevent real entry-point loading to isolate this test
        with patch.object(clean_registry, "_load_entry_point_plugins", return_value=[]):
            names1 = clean_registry.discover()
            names2 = clean_registry.discover()
            assert names1 == names2
            # _loaded flag should prevent a second pass
            assert clean_registry._loaded is True

    def test_load_entry_point_plugins_missing_entry_points_module(self):
        """_load_entry_point_plugins handles importlib.metadata missing entry_points."""
        reg = plugins.PluginRegistry()
        with patch("dbk.plugins.importlib.metadata", None):
            loaded = reg._load_entry_point_plugins()
            assert loaded == []

    def test_load_entry_point_plugins_empty_group(self):
        """_load_entry_point_plugins handles an empty entry point group gracefully."""
        reg = plugins.PluginRegistry()

        class FakeEntryPoints(list):
            pass

        fake_eps = FakeEntryPoints()

        with patch("dbk.plugins.importlib.metadata.entry_points", return_value=fake_eps):
            loaded = reg._load_entry_point_plugins()
            assert loaded == []

    def test_load_dir_plugins_nonexistent_dir(self):
        """_load_dir_plugins returns [] for a non-existent directory."""
        reg = plugins.PluginRegistry()
        loaded = reg._load_dir_plugins(Path("/nonexistent/path/xyz123"))
        assert loaded == []


# ----------------------------------------------------------------------
#  Directory-plugin loading (real files via tmp_path)
# ----------------------------------------------------------------------


class TestLoadDirPlugins:
    def test_load_dir_plugin_package_with_plugin_py(self, tmp_path):
        """_load_dir_plugins loads <name>/plugin.py as a PluginABC subclass."""
        plugin_pkg = tmp_path / "test_dir_plugin"
        plugin_pkg.mkdir()
        (plugin_pkg / "plugin.py").write_text(
            "from dbk.plugins import PluginABC, hookimpl\n"
            "\n"
            "class TestDirPlugin(PluginABC):\n"
            "    name = 'test_dir_plugin'\n"
            "    version = '0.1.0'\n"
            "\n"
            "    def dbk_tool_register(self, registry):\n"
            "        pass\n"
        )

        reg = plugins.PluginRegistry()
        loaded = reg._load_dir_plugins(tmp_path)

        assert "test_dir_plugin" in loaded
        assert reg.plugin_count == 1
        assert reg._plugins[0].name == "test_dir_plugin"

    def test_load_dir_plugin_with_hookimpl_function(self, tmp_path):
        """_load_dir_plugins collects @hookimpl-decorated standalone functions."""
        plugin_pkg = tmp_path / "test_func_plugin"
        plugin_pkg.mkdir()
        (plugin_pkg / "plugin.py").write_text(
            "from dbk.plugins import hookimpl\n"
            "\n"
            "@hookimpl\n"
            "def dbk_tool_register(registry):\n"
            "    pass\n"
            "\n"
            "@hookimpl\n"
            "def dbk_system_prompt(parts):\n"
            "    parts.append('[test]')\n"
        )

        reg = plugins.PluginRegistry()
        loaded = reg._load_dir_plugins(tmp_path)

        assert "test_func_plugin" in loaded
        # Standalone hooks should be registered in _hook_funcs
        assert len(reg._hook_funcs["dbk_tool_register"]) >= 1
        assert len(reg._hook_funcs["dbk_system_prompt"]) >= 1

    def test_load_dir_plugin_skips_private_dirs(self, tmp_path):
        """_load_dir_plugins skips directories starting with _ or ."""
        (tmp_path / "_private").mkdir()
        (tmp_path / "_private" / "plugin.py").write_text("x = 1")
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "plugin.py").write_text("x = 1")

        reg = plugins.PluginRegistry()
        loaded = reg._load_dir_plugins(tmp_path)
        assert loaded == []

    def test_load_dir_plugin_skips_without_init_or_plugin_py(self, tmp_path):
        """A directory without __init__.py or plugin.py is skipped."""
        (tmp_path / "no_plugin").mkdir()
        (tmp_path / "no_plugin" / "other.py").write_text("x = 1")

        reg = plugins.PluginRegistry()
        loaded = reg._load_dir_plugins(tmp_path)
        assert loaded == []

    def test_load_dir_plugin_single_py_file(self, tmp_path):
        """A bare .py file (not starting with _) is loaded as a plugin module."""
        (tmp_path / "standalone_plugin.py").write_text(
            "from dbk.plugins import PluginABC\n"
            "\n"
            "class StandalonePlugin(PluginABC):\n"
            "    name = 'standalone_plugin'\n"
            "    version = '0.2.0'\n"
            "\n"
            "    def dbk_tool_register(self, registry):\n"
            "        pass\n"
        )

        reg = plugins.PluginRegistry()
        loaded = reg._load_dir_plugins(tmp_path)

        assert "standalone_plugin" in loaded
        assert reg.plugin_count == 1


# ----------------------------------------------------------------------
#  Hook dispatch
# ----------------------------------------------------------------------


class TestHookDispatch:
    def test_apply_tool_hooks_calls_register(self, clean_registry):
        """apply_tool_hooks calls dbk_tool_register on each plugin."""

        class ToolPlugin(plugins.PluginABC):
            name = "tool_test"
            version = "0.1.0"
            called = False

            def dbk_tool_register(self, registry):
                ToolPlugin.called = True

        plugin = ToolPlugin()
        clean_registry.register(plugin)
        mock_registry = MagicMock()
        clean_registry.apply_tool_hooks(mock_registry)
        assert ToolPlugin.called is True

    def test_apply_agent_init_hooks_calls_agent_init(self, clean_registry):
        """apply_agent_init_hooks calls dbk_agent_init on each plugin."""

        class InitPlugin(plugins.PluginABC):
            name = "init_test"
            version = "0.1.0"
            received = None

            def dbk_tool_register(self, registry):
                pass

            def dbk_agent_init(self, agent):
                InitPlugin.received = agent

        plugin = InitPlugin()
        clean_registry.register(plugin)
        fake_agent = object()
        clean_registry.apply_agent_init_hooks(fake_agent)
        assert InitPlugin.received is fake_agent

    def test_get_api_routes_aggregates(self, clean_registry):
        """get_api_routes aggregates routes from all plugins."""

        class RoutePlugin(plugins.PluginABC):
            name = "route_test"
            version = "0.1.0"

            def dbk_tool_register(self, registry):
                pass

            def dbk_api_routes(self):
                return [("/a", "GET", {"handler": lambda: "a"}),
                        ("/b", "POST", {"handler": lambda: "b"})]

        class RoutePlugin2(plugins.PluginABC):
            name = "route_test2"
            version = "0.2.0"

            def dbk_tool_register(self, registry):
                pass

            def dbk_api_routes(self):
                return [("/c", "GET", {"handler": lambda: "c"})]

        clean_registry.register(RoutePlugin())
        clean_registry.register(RoutePlugin2())
        routes = clean_registry.get_api_routes()
        assert len(routes) == 3
        paths = [r[0] for r in routes]
        assert "/a" in paths
        assert "/b" in paths
        assert "/c" in paths

    def test_apply_tool_hooks_with_standalone_hookimpl(self, tmp_path):
        """apply_tool_hooks also calls standalone @hookimpl functions."""
        plugin_pkg = tmp_path / "stand_hook"
        plugin_pkg.mkdir()
        (plugin_pkg / "plugin.py").write_text(
            "from dbk.plugins import hookimpl\n"
            "\n"
            "@hookimpl\n"
            "def dbk_tool_register(registry):\n"
            "    registry.called = True\n"
        )

        reg = plugins.PluginRegistry()
        reg._load_dir_plugins(tmp_path)
        mock_registry = MagicMock()
        reg.apply_tool_hooks(mock_registry)
        assert mock_registry.called is True

    def test_build_system_prompt(self, clean_registry):
        """build_system_prompt calls dbk_system_prompt hooks and joins with newlines."""

        class PromptPlugin(plugins.PluginABC):
            name = "prompt_test"
            version = "0.1.0"

            def dbk_tool_register(self, registry):
                pass

            def dbk_system_prompt(self, parts):
                parts.append("[from plugin]")

        clean_registry.register(PromptPlugin())
        result = clean_registry.build_system_prompt("base prompt")
        assert "[from plugin]" in result
        assert "base prompt" in result


# ----------------------------------------------------------------------
#  Singleton
# ----------------------------------------------------------------------


class TestSingleton:
    def test_get_plugin_registry_returns_same_instance(self):
        """get_plugin_registry() returns the same object on repeated calls."""
        plugins._global_registry = None
        a = plugins.get_plugin_registry()
        b = plugins.get_plugin_registry()
        assert a is b
        plugins._global_registry = None

    def test_get_plugin_registry_returns_new_after_reset(self):
        """Resetting _global_registry causes get_plugin_registry to return a new object."""
        plugins._global_registry = None
        a = plugins.get_plugin_registry()
        plugins._global_registry = None
        b = plugins.get_plugin_registry()
        assert a is not b
        plugins._global_registry = None


# ----------------------------------------------------------------------
#  Integration: full discover with fake entry point
# ----------------------------------------------------------------------


class TestFullDiscover:
    def test_discover_with_fake_entry_point(self, clean_registry, tmp_path):
        """discover() loads a plugin via a mocked entry point."""
        # Create a temp plugin package
        plugin_pkg = tmp_path / "ep_fake_plugin"
        plugin_pkg.mkdir()
        (plugin_pkg / "__init__.py").write_text(
            "from dbk.plugins import PluginABC\n"
            "\n"
            "class EpFakePlugin(PluginABC):\n"
            "    name = 'ep_fake_plugin'\n"
            "    version = '1.0.0'\n"
            "\n"
            "    def dbk_tool_register(self, registry):\n"
            "        pass\n"
        )
        sys.path.insert(0, str(tmp_path))

        class FakeEntryPoint:
            module = "ep_fake_plugin"

            def __repr__(self):
                return "<FakeEntryPoint ep_fake_plugin>"

        fake_eps = [FakeEntryPoint()]

        with patch("dbk.plugins.importlib.metadata.entry_points", return_value=fake_eps):
            loaded = clean_registry.discover()
            assert "ep_fake_plugin" in loaded
            assert clean_registry.plugin_count == 1

        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))
