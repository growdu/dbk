# DBK AI Agent Core - Specification

## Overview
AI Agent core for DBK (Database Kernel observability CLI). Adds LLM-powered intent recognition, workflow orchestration, tool registry, and session persistence.

## Architecture

### Provider Layer (`dbk/providers/`)
- `base.py` - Abstract `BaseProvider` ABC with `chat()` and `chat_stream()` methods
- `mock.py` - `MockProvider` for offline/development use
- `openai.py` - `OpenAIProvider` supporting both openai<1.0 and >=1.0 client APIs
- `anthropic.py` - `AnthropicProvider` for Claude models
- `__init__.py` - Auto-selects provider based on env vars (`DBK_PROVIDER`, `DBK_OPENAI_API_KEY`, `DBK_ANTHROPIC_API_KEY`, `DBK_MODEL`)

### Agent Core (`dbk/agent/`)
- `state.py` - `AgentState` dataclass holding session context, workflow state, history
- `tools.py` - `Tool` dataclass + `ToolRegistry` mapping 10+ DBK capabilities to callable tools
- `intent.py` - `IntentRecognizer` with keyword + LLM hybrid intent detection
- `session.py` - `SessionManager` for per-session context
- `workflow.py` - `WorkflowStateMachine` for requirements→design→implement→test→runtime→doc→ops→done
- `core.py` - `Agent` class orchestrating all components
- `session_store.py` - SQLite-backed `SessionStore` for session persistence

### CLI Integration (`dbk/cli_agent.py`)
- New `dbk agent` subcommand with `--session`, `--model`, `--provider` options
- REPL loop for interactive conversations
- File-based session loading

## Environment Variables
| Variable | Description |
|---|---|
| `DBK_PROVIDER` | Force provider: "openai", "anthropic", "mock" |
| `DBK_MODEL` | Model name override |
| `DBK_OPENAI_API_KEY` | OpenAI API key |
| `DBK_ANTHROPIC_API_KEY` | Anthropic API key |

## Tool Registry (10+ Tools)
1. `collect_metrics` - Collect runtime metrics
2. `query_metrics` - Query stored metrics
3. `health_check` - Collector health check
4. `diagnose_incident` - Latency incident diagnosis
5. `run_trace` - Execute trace profile
6. `cleanup_data` - Runtime data cleanup
7. `start_collector_daemon` - Start collector daemon
8. `stop_collector_daemon` - Stop collector daemon
9. `daemon_status` - Check daemon status
10. `validate_config` - Validate DBK configuration
11. `list_daemons` - List running daemons
12. `cleanup_report` - Get cleanup report

## Workflow States
`requirements` → `design` → `implement` → `test` → `runtime` → `doc` → `ops` → `done`

## Error Handling
- Exponential backoff retry: 3 attempts, base 1s, max 10s
- 60s request timeout
- Graceful degradation: mock provider if no API key
- Both openai<1.0 and >=1.0 client API compatibility
