# DBK Agent — Plugin System

## Overview

The DBK Agent plugin system allows extending the agent with custom tools, system
prompt modifications, API routes, and lifecycle hooks — all without modifying the
core DBK codebase.

## Quick Example

```python
from dbk.agent.tools import Tool
from dbk.plugins import PluginABC, hookimpl

class MyPlugin(PluginABC):
    name = "my_plugin"
    version = "1.0.0"

    def dbk_tool_register(self, registry):
        registry.register(Tool(
            name="my_custom_tool",
            description="Does something useful",
            parameters={"type": "object", "properties": {}},
            callable=my_function,
            category="general",
        ))

    def dbk_system_prompt(self, parts: list[str]):
        parts.append(
            "[Custom instruction] When the user asks about X, do Y."
        )
```

## Hooks

All hooks are optional. Implement only what you need.

### `dbk_tool_register(registry)`

Called once during `Agent.__init__()`. Register tools with the `ToolRegistry`.

```python
def dbk_tool_register(self, registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="pg_stat_activity",
        description="Query pg_stat_activity",
        parameters={"type": "object", "properties": {"instance": {"type": "string"}}},
        callable=tool_pg_stat_activity,
        category="diagnose",
    ))
```

### `dbk_system_prompt(parts)`

Append text fragments to the agent's system prompt. The base prompt is passed
first; plugins append after it.

```python
def dbk_system_prompt(self, parts: list[str]) -> None:
    parts.append(
        "[Audit mode] All queries to pg_stat_activity must include WHERE datname IS NOT NULL."
    )
```

### `dbk_agent_init(agent)`

Called after the `Agent` instance is constructed, after tools are registered.
Use for custom agent configuration.

```python
def dbk_agent_init(self, agent) -> None:
    agent._custom_attribute = MyState()
```

### `dbk_post_message(message, result)`

Called after every user message is processed. Useful for logging, metrics, or
side-effects based on responses.

```python
def dbk_post_message(self, message: str, result: dict) -> None:
    logger.info("Message processed: intent=%s", result.get("intent"))
```

### `dbk_api_routes()`

Return additional FastAPI routes. Each entry is a `(path, method, kwargs)` tuple.

```python
def dbk_api_routes(self) -> list[tuple[str, str, dict]]:
    return [
        ("/my-plugin/health", "GET", {"handler": my_health_handler}),
        ("/my-plugin/query", "POST", {"handler": my_query_handler}),
    ]
```

### `dbk_cleanup()`

Called when the agent is shutting down. Use for releasing resources.

```python
def dbk_cleanup(self) -> None:
    self._connection.close()
```

## Standalone Hook Functions

For simple plugins that don't need a full class, use the `@hookimpl` decorator
on standalone functions:

```python
from dbk.plugins import hookimpl

@hookimpl
def dbk_tool_register(registry):
    registry.register(Tool(...))

@hookimpl
def dbk_system_prompt(parts):
    parts.append("[Prometheus plugin] Export metrics at /metrics")
```

Both class-based and function-based hooks are automatically collected from the
same module.

## Plugin Discovery

Plugins are discovered in this order:

1. **`dbk.plugins` entry points** — installed packages that declare
   `entry_points = {"dbk.plugins": ["my-plugin = my_plugin.plugin:plugin_instance"]}`
2. **`DBK_PLUGIN_DIR`** — directory set in the environment variable
3. **`~/.dbk/plugins/`** — user-local plugin directory

Directory plugins can be structured as:
- A Python package: `~/.dbk/plugins/my_plugin/__init__.py`
- A single module: `~/.dbk/plugins/my_plugin.py`

## Enabling the Prometheus Sample Plugin

```bash
export DBK_PLUGIN_DIR=$HOME/dbk_plugins/samples
# Or symlink:
ln -s /work/ai/dbk/dbk_plugins/samples ~/.dbk/plugins/samples

# Restart the agent or API server.
# GET /metrics will return Prometheus-formatted metrics.
```

## Introspection

```python
from dbk.plugins import get_plugin_registry

reg = get_plugin_registry()
for plugin in reg.list_plugins():
    print(plugin["name"], plugin["version"], plugin["hooks"])

# 0 plugin routes:
for path, method, kwargs in reg.get_api_routes():
    print(f"  {method} {path}")
```

## Error Handling

Plugin errors are caught and logged as warnings — a failing plugin never crashes
the agent. The agent continues operating with the remaining plugins.

## Sample Plugins

Two sample plugins ship with DBK:

| Plugin | File | Hooks Used | Description |
|--------|------|------------|-------------|
| `prometheus_exporter` | `dbk_plugins/samples/plugin.py` | `dbk_post_message`, `dbk_api_routes` | Exposes `/metrics` endpoint |
| `pgaudit_helper` | `dbk_plugins/samples/plugin.py` | `dbk_tool_register` | Registers `pgaudit_summary` tool |
