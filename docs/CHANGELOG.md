# Changelog

All notable changes to DBK will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-04

### Added

- **CLI**: Full `dbk` command with subcommands: `init`, `validate`, `collect`,
  `metrics`, `trace`, `diagnose`, `runtime`, `alert`, `agent`, `api-server`
- **Metrics collection**: 10 metric types (query latency, wait/lock ratio,
  IO latency, buffer hit ratio, connection counts, transaction rollback ratio,
  checkpoint write latency) via mock and pgstat backends
- **Workflow agent**: `dbk run "<intent>"` natural-language entry point that
  auto-maps to workflow stages (requirements → design → implement → test →
  runtime → doc → ops → done)
- **真实 LLM**: AnthropicProvider with MiniMax API (socks5) integration;
  Claude Haiku via `ANTHROPIC_AUTH_TOKEN` environment variable
- **Multi-format tool calling**: Supports both JSON `{name:...}` and Clojure
  `{tool=>...}` output formats from LLM
- **SubAgent framework**: SubAgent / MainAgent / SubAgentPool / SubAgentExecutor
  with 49 passing tests
- **AIOps alerting**: AlertStore, AlertEngine, AlertRule, AlertEvent with
  AgentResponder that automatically triggers diagnostic sessions on alert
  firing; `dbk alert daemon start --enable-agent`
- **SQL diagnostics**: SQL fingerprint normalization (`normalize_sql()`),
  EXPLAIN generation (`build_explain_sql()`), lock contention and replication
  bottleneck specialist diagnosticians
- **eBPF privilege escalation**: 4-path escalation engine (root → pkexec →
  sudo → none) with polkit policy files, scoped to `/usr/bin/bpftrace` only,
  and audit trail via `trace_approval_audit` table
- **Audit trail**: `trace_approval_audit` table (12 fields: task_id, username,
  action_id, command_json, mode, escalation, approved_by_cli, etc.)
- **SDK**: `from dbk import DBK, DBKClient` public entry point; DBKClient with
  collect, health_check, diagnose, evaluate_alerts, store_metric, get_metrics,
  daemon_start/stop/list/status, cleanup, chat, stream_chat, sessions,
  workflow methods
- **HTTP API server**: FastAPI server with /metrics, /health, /daemons,
  /sessions, /chat, /trace endpoints; `dbk api-server`
- **Persistence**: SQLite-based RuntimeStore and SessionStore
- **Cleanup daemon**: Safety thresholds (24h floor, 10万行 max per run) and
  two-phase graceful shutdown
- **CI/CD**: pre-commit hooks + GitHub Actions (lint, type-check, pytest on
  Python 3.10/3.11/3.12)
- **Documentation**: `docs/详细设计文档.md` (architecture), `docs/plan.md` (progress)

### Fixed

- Tool call parsing: `_parse_tool_calls` now handles both Haiku JSON format
  and Clojure `{tool=>...}` format
- Alert timezone handling: SQLite string comparison with ISO 8601 UTC
  timestamps now strips microseconds for correctness
- Cleanup safety floor: `--safety-floor-hours` prevents accidental mass
  deletion of recent data
- Daemon graceful shutdown: two-phase SIGTERM (3s grace) + SIGKILL fallback
- Polkit denial: pkexec returncode 126 now surfaces as `escalation_failed`
  result rather than silently falling through

### Changed

- `dbk/__init__.py`: Added lazy re-exports for `DBK`, `DBKClient`,
  `get_default_client`, `SDKConfig` so `from dbk import DBK` works
- `dbk/cli.py`: `cmd_trace_run()` now writes audit record for every
  execute=True invocation; `PROFILE_COMMANDS` properly imported
- `dbk/storage.py`: `trace_approval_audit` table and `insert_trace_audit()`
  method added
- `dbk/tracing.py`: Complete rewrite of privilege escalation logic with
  `EscalationResult` dataclass and `_escalate()` engine; `summary.json`
  now includes `escalation` field

## [0.0.0] - 2026-04-?? - Initial skeleton
