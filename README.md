# DBK Agent

Database Kernel observability AI Agent — CLI + REST API + Web UI.

## Overview

DBK Agent is an LLM-powered observability assistant for PostgreSQL kernel metrics.
It provides:

- **CLI** — Metrics collection, latency diagnosis, trace profiles, collector daemons, and runtime cleanup
- **Agent REPL** — Interactive LLM-powered assistant for natural-language observability tasks
- **REST API** — HTTP endpoints for chat, sessions, memory, and plugin routes
- **Web UI** — Browser-based chat interface (see `frontend/`)
- **Plugin system** — Extensible tools, system prompts, API routes, and lifecycle hooks

## Quick Start

### CLI

```bash
# Init
python3 -m dbk init

# Collect metrics
python3 -m dbk collect --instance pg-main-01 --source mock
python3 -m dbk collect health --source pgstat --dsn "postgresql://user:***@127.0.0.1:5432/postgres"

# Daemons
python3 -m dbk collect daemon start --instance pg-main-01 --source mock --interval-sec 15 --priority 75 --tags prod
python3 -m dbk collect daemon status
python3 -m dbk collect daemon list
python3 -m dbk collect daemon stop --instance pg-main-01

# Metrics queries
python3 -m dbk metrics --metric query.p95_latency_ms --instance pg-main-01 --limit 10

# Trace profiles
python3 -m dbk trace profiles
python3 -m dbk trace run --profile cpu-hotpath --task-id demo-1 --duration 30

# Diagnosis
python3 -m dbk diagnose latency --instance pg-main-01 --task-id incident-1

# Cleanup
python3 -m dbk runtime cleanup --older-than-hours 168 --dry-run
python3 -m dbk runtime cleanup-report --limit 50 --window-hours 24
```

### Agent REPL (LLM-powered)

```bash
# Interactive chat with the agent
python3 -m dbk agent --interactive

# Show agent info
python3 -m dbk agent info

# List sessions
python3 -m dbk agent sessions
```

The REPL supports commands: `help`, `info`, `workflow`, `session`, `memory`, `clear`, `exit`.

### REST API Server

```bash
# Start the API server
python3 -m dbk api-server --port 8080

# Or programmatically:
python3 -c "from dbk.api_server import run_server; run_server()"
```

Endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (checks agent) |
| GET | `/info` | Agent configuration and capabilities |
| POST | `/sessions` | Create a new session |
| GET | `/sessions` | List all sessions |
| GET | `/sessions/{id}` | Get session details |
| GET | `/sessions/{id}/history` | Get conversation history |
| DELETE | `/sessions/{id}` | Delete a session |
| POST | `/sessions/{id}/workflow` | Advance workflow stage |
| POST | `/chat` | Send a chat message (blocking) |
| POST | `/chat/stream` | Send a chat message (SSE streaming) |
| POST | `/memory/facts` | Store a fact |
| GET | `/memory/facts` | Recall facts |
| DELETE | `/memory/facts/{id}` | Delete a fact |
| POST | `/memory/summaries` | Record a summary |
| GET | `/memory/summaries` | Get summaries |
| GET | `/memory/episodes` | Recall episodic memory |
| GET | `/memory/context` | Build memory context string |
| POST | `/memory/prune` | Prune old episodic entries |

Example:

```bash
curl -X POST http://127.0.0.1:8080/sessions \
  -H 'Content-Type: application/json' \
  -d '{"goal":"latency investigation"}'

curl -X POST "http://127.0.0.1:8080/chat?session_id=$SID" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Diagnose latency for pg-main-01"}'
```

### Web UI

A browser-based chat interface for the DBK Agent REST API.

```bash
# Start the API server (port 8080)
python3 -m dbk api-server --port 8080 &

# Start a static file server (port 8081)
cd frontend && python3 -m http.server 8081 &

# Open: http://localhost:8081/?api=http://localhost:8080
```

Features:
- Session management (create, load, list)
- Chat with streaming support
- Quick action buttons (collect, diagnose, health, daemon status)
- Tool registry display
- Workflow stage tracker
- Memory panel (facts, summaries, episodes)
- Session history viewer

### Plugin System

DBK supports a pluggable architecture. Plugins can:
- Register new tools with the agent's ToolRegistry
- Append text to the system prompt
- Add FastAPI routes (e.g. `/metrics` for Prometheus)
- React to messages after they are processed
- Initialize and cleanup on agent lifecycle

```python
from dbk.plugins import PluginABC, hookimpl

class MyPlugin(PluginABC):
    name = "my_plugin"
    version = "1.0.0"

    def dbk_tool_register(self, registry):
        registry.register(MyTool(...))

    def dbk_system_prompt(self, parts):
        parts.append("[Custom DBK instruction]")
```

See `dbk/plugins.py` for the full hook specification and `dbk_plugins/samples/` for examples.

Plugins are auto-discovered from:
- `dbk.plugins` entry points (installed packages)
- `$DBK_PLUGIN_DIR/` directory
- `~/.dbk/plugins/`

### End-to-End Demo

Run the complete demo covering all features:

```bash
./demo.sh
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DBK_PROVIDER` | Force provider: `openai`, `anthropic`, or `mock` |
| `DBK_MODEL` | Model name override |
| `DBK_OPENAI_API_KEY` | OpenAI API key |
| `DBK_ANTHROPIC_API_KEY` | Anthropic API key |
| `DBK_PG_DSN` | PostgreSQL connection string for `pgstat` source |
| `DBK_ROOT` | Override `.dbk/` directory location |
| `DBK_PLUGIN_DIR` | Additional plugin search directory |

## Testing

```bash
# Unit tests
python3 -m pytest -q

# Docker integration tests
DBK_RUN_DOCKER_TESTS=1 DBK_PG_DOCKER_VERSIONS=14,15,16 \
  python3 -m pytest -q tests/test_pg_integration_docker.py
```

## PostgreSQL Compatibility

| PG | `pg_stat_statements` | `pg_stat_io` | `query.p95_latency_ms` | `io.read_latency_ms` |
|----|-----------------------|--------------|------------------------|----------------------|
| 14 | Optional | Unsupported | `pg_stat_statements` / activity fallback | Unavailable |
| 15 | Optional | Unsupported | `pg_stat_statements` / activity fallback | Unavailable |
| 16+ | Optional | Supported | `pg_stat_statements` / activity fallback | `pg_stat_io` fallback |

## Architecture

```
dbk/
  agent/          Agent core (core.py, tools.py, session.py, workflow.py, intent.py, memory.py, repl.py)
  providers/       LLM providers (base.py, mock.py, openai.py, anthropic.py)
  api_server.py   FastAPI REST server
  cli.py          CLI entry point
  cli_agent.py    Agent CLI (REPL, info, session management)
  plugins.py      Plugin system (PluginABC, PluginRegistry, hookimpl)
dbk_plugins/
  samples/        Sample plugins (Prometheus exporter, pgAudit helper)
frontend/
  index.html      Web UI
  app.css         Web UI styles
  app.js          Web UI JavaScript
doc/
  PLUGIN_SYSTEM.md    Plugin system documentation
  WEB_UI.md           Web UI documentation
  API.md              REST API reference
```

## Current Limitations

- PostgreSQL collection depends on `psycopg`; uninstalled → graceful warning
- Missing `pg_stat_statements` / `pg_stat_io` → 0-fill + warning
- Trace is simulated by default; `--execute --approve-privileged` for real traces
- Cleanup daemon is single-instance (global) task model
