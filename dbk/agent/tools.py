"""Tool registry for DBK agent."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dbk.config import artifacts_root, runtime_db_path


@dataclass
class Tool:
    """Represents a callable tool available to the agent."""

    name: str
    description: str
    parameters: dict[str, Any]
    callable: Callable[..., Any]
    category: str = "general"

    def execute(self, **params: Any) -> dict[str, Any]:
        """Execute the tool with given parameters."""
        try:
            result = self.callable(**params)
            return {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


def _store() -> Any:
    from dbk.storage import RuntimeStore
    store = RuntimeStore(runtime_db_path())
    store.init_schema()
    return store


# ----------------------------------------------------------------------
# Tool implementations that wrap existing DBK functionality.
# ----------------------------------------------------------------------


def tool_collect_metrics(instance: str = "pg-main-01", source: str = "mock", dsn: str | None = None) -> dict[str, Any]:
    """Collect runtime metrics and store them."""
    from dbk.collectors import collect_mock_runtime_metrics
    from dbk.pg_collectors import collect_pg_runtime_metrics, PgCollectorError

    if source == "mock":
        events = collect_mock_runtime_metrics(instance=instance)
        store = _store()
        count = store.insert_events(events)
        return {"collected": count, "instance": instance, "source": source}
    if source == "pgstat":
        resolved_dsn = dsn or os.environ.get("DBK_PG_DSN", "")
        if not resolved_dsn:
            raise ValueError("Missing DSN for pgstat source")
        result = collect_pg_runtime_metrics(instance=instance, dsn=resolved_dsn)
        store = _store()
        count = store.insert_events(result.events)
        return {"collected": count, "warnings": result.warnings, "instance": instance}
    raise ValueError(f"Unknown source: {source}")


def tool_query_metrics(
    metric: str,
    instance: str | None = None,
    limit: int = 20,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> dict[str, Any]:
    """Query stored metrics."""
    store = _store()
    if from_ts:
        rows = store.query_metric_range(
            metric=metric,
            instance=instance,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
        )
        return {"metric": metric, "rows": list(rows), "mode": "range"}
    else:
        rows = store.query_latest_metric(metric=metric, instance=instance, limit=limit)
        return {"metric": metric, "rows": list(rows), "mode": "latest"}


def tool_health_check(source: str = "mock", dsn: str | None = None) -> dict[str, Any]:
    """Check collector health/readiness."""
    from dbk.pg_collectors import collect_pg_health

    if source != "pgstat":
        return {"ok": True, "degraded": False, "details": {"collector": source}}
    resolved_dsn = dsn or os.environ.get("DBK_PG_DSN", "")
    if not resolved_dsn:
        raise ValueError("Missing DSN for pgstat health check")
    report = collect_pg_health(dsn=resolved_dsn)
    return report.to_dict()


def tool_diagnose_incident(
    instance: str,
    task_id: str | None = None,
    auto_trace: bool = False,
    thresholds_file: str | None = None,
) -> dict[str, Any]:
    """Diagnose a latency incident."""
    from dbk.diagnose import diagnose_latency_incident
    from dbk.thresholds import load_thresholds

    thresholds = None
    if thresholds_file:
        thresholds = load_thresholds(Path(thresholds_file))

    store = _store()
    result = diagnose_latency_incident(
        store=store,
        instance=instance,
        task_id=task_id,
        artifacts_root=artifacts_root(),
        auto_trace=auto_trace,
        thresholds=thresholds,
    )
    return {
        "verdict": result.verdict,
        "findings": result.findings,
        "evidence_bundle": result.evidence_bundle,
        "trace_summary": result.trace_summary,
    }


def tool_run_trace(
    profile: str,
    task_id: str,
    duration_sec: int = 5,
    execute: bool = False,
) -> dict[str, Any]:
    """Execute a trace profile."""
    from dbk.tracing import run_trace_profile

    result = run_trace_profile(
        profile=profile,
        task_id=task_id,
        duration_sec=duration_sec,
        artifacts_root=artifacts_root(),
        execute=execute,
    )
    return {
        "profile": profile,
        "task_id": task_id,
        "stdout_path": str(result.stdout_path),
        "summary_path": str(result.summary_path),
        "summary": result.artifact.summary_json,
    }


def tool_cleanup_data(
    older_than_hours: float = 168.0,
    instance: str | None = None,
    dry_run: bool = True,
    skip_trace_db: bool = False,
    skip_artifacts: bool = False,
    vacuum: bool = False,
) -> dict[str, Any]:
    """Cleanup runtime data."""
    from dbk.runtime_cleanup import cleanup_runtime_data

    store = _store()
    summary = cleanup_runtime_data(
        store=store,
        older_than_hours=older_than_hours,
        instance=instance,
        dry_run=dry_run,
        skip_trace_db=skip_trace_db,
        skip_artifacts=skip_artifacts,
        vacuum=vacuum,
    )
    return summary.to_dict()


def tool_start_collector_daemon(
    instance: str = "pg-main-01",
    source: str = "mock",
    interval_sec: int = 15,
    priority: int = 50,
    dsn: str | None = None,
) -> dict[str, Any]:
    """Start a collector daemon."""
    from dbk.collector_daemon import start_daemon

    resolved_dsn = dsn
    if source == "pgstat" and not dsn:
        resolved_dsn = os.environ.get("DBK_PG_DSN")

    state = start_daemon(
        instance=instance,
        source=source,
        interval_sec=interval_sec,
        priority=priority,
        tags=[],
        max_collections_per_minute=None,
        max_running=None,
        preempt_lower_priority=False,
        dsn=resolved_dsn,
        cwd=Path.cwd(),
    )
    return {"started": True, "pid": state.pid, "instance": state.instance}


def tool_stop_collector_daemon(instance: str | None = None, all_instances: bool = False) -> dict[str, Any]:
    """Stop a collector daemon."""
    from dbk.collector_daemon import stop_all_daemons, stop_daemon

    if all_instances:
        return stop_all_daemons(cwd=Path.cwd())
    if instance:
        return stop_daemon(instance=instance, cwd=Path.cwd())
    raise ValueError("Must provide instance or set all_instances=True")


def tool_daemon_status(instance: str | None = None) -> dict[str, Any]:
    """Check daemon status."""
    from dbk.collector_daemon import daemon_status

    return daemon_status(instance=instance, cwd=Path.cwd())


def tool_validate_config() -> dict[str, Any]:
    """Validate DBK configuration."""
    from dbk.config import validate_config

    result = validate_config()
    return result.as_dict()


def tool_list_daemons(
    tag: str | None = None,
    source: str | None = None,
    instance_pattern: str | None = None,
    min_priority: int | None = None,
) -> dict[str, Any]:
    """List running daemons."""
    from dbk.collector_daemon import list_daemons

    daemons = list_daemons(
        cwd=Path.cwd(),
        include_stale=True,
        tag=tag,
        source=source,
        instance_pattern=instance_pattern,
        min_priority=min_priority,
    )
    return {"daemons": daemons}


def tool_cleanup_report(limit: int = 100, window_hours: int = 24) -> dict[str, Any]:
    """Get cleanup report."""
    from dbk.runtime_cleanup_daemon import build_cleanup_report, cleanup_daemon_status

    payload = build_cleanup_report(limit=limit, window_hours=window_hours, cwd=Path.cwd())
    daemon_payload = cleanup_daemon_status(cwd=Path.cwd())
    payload["daemon"] = daemon_payload
    return payload


# ----------------------------------------------------------------------
# Tool Registry
# ----------------------------------------------------------------------


class ToolRegistry:
    """Registry of all tools available to the agent."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        tools = [
            Tool(
                name="collect_metrics",
                description="Collect runtime metrics from mock or pgstat source and store them",
                parameters={
                    "type": "object",
                    "properties": {
                        "instance": {"type": "string", "default": "pg-main-01"},
                        "source": {"type": "string", "default": "mock", "enum": ["mock", "pgstat"]},
                        "dsn": {"type": "string"},
                    },
                },
                callable=tool_collect_metrics,
                category="collect",
            ),
            Tool(
                name="query_metrics",
                description="Query stored metrics from the runtime SQLite store",
                parameters={
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "instance": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                        "from_ts": {"type": "string"},
                        "to_ts": {"type": "string"},
                    },
                    "required": ["metric"],
                },
                callable=tool_query_metrics,
                category="query",
            ),
            Tool(
                name="health_check",
                description="Check collector health and readiness",
                parameters={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "default": "mock"},
                        "dsn": {"type": "string"},
                    },
                },
                callable=tool_health_check,
                category="diagnose",
            ),
            Tool(
                name="diagnose_incident",
                description="Diagnose a latency incident for a given instance",
                parameters={
                    "type": "object",
                    "properties": {
                        "instance": {"type": "string"},
                        "task_id": {"type": "string"},
                        "auto_trace": {"type": "boolean", "default": False},
                        "thresholds_file": {"type": "string"},
                    },
                    "required": ["instance"],
                },
                callable=tool_diagnose_incident,
                category="diagnose",
            ),
            Tool(
                name="run_trace",
                description="Execute a trace profile (strace, perf, etc.)",
                parameters={
                    "type": "object",
                    "properties": {
                        "profile": {"type": "string"},
                        "task_id": {"type": "string"},
                        "duration_sec": {"type": "integer", "default": 5},
                        "execute": {"type": "boolean", "default": False},
                    },
                    "required": ["profile", "task_id"],
                },
                callable=tool_run_trace,
                category="diagnose",
            ),
            Tool(
                name="cleanup_data",
                description="Cleanup retained runtime data (events, traces, artifacts)",
                parameters={
                    "type": "object",
                    "properties": {
                        "older_than_hours": {"type": "number", "default": 168.0},
                        "instance": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": True},
                        "skip_trace_db": {"type": "boolean", "default": False},
                        "skip_artifacts": {"type": "boolean", "default": False},
                        "vacuum": {"type": "boolean", "default": False},
                    },
                },
                callable=tool_cleanup_data,
                category="ops",
            ),
            Tool(
                name="start_collector_daemon",
                description="Start a background collector daemon",
                parameters={
                    "type": "object",
                    "properties": {
                        "instance": {"type": "string", "default": "pg-main-01"},
                        "source": {"type": "string", "default": "mock"},
                        "interval_sec": {"type": "integer", "default": 15},
                        "priority": {"type": "integer", "default": 50},
                        "dsn": {"type": "string"},
                    },
                },
                callable=tool_start_collector_daemon,
                category="ops",
            ),
            Tool(
                name="stop_collector_daemon",
                description="Stop a collector daemon",
                parameters={
                    "type": "object",
                    "properties": {
                        "instance": {"type": "string"},
                        "all_instances": {"type": "boolean", "default": False},
                    },
                },
                callable=tool_stop_collector_daemon,
                category="ops",
            ),
            Tool(
                name="daemon_status",
                description="Check if a collector daemon is running",
                parameters={
                    "type": "object",
                    "properties": {
                        "instance": {"type": "string"},
                    },
                },
                callable=tool_daemon_status,
                category="ops",
            ),
            Tool(
                name="validate_config",
                description="Validate DBK configuration and environment",
                parameters={"type": "object", "properties": {}},
                callable=tool_validate_config,
                category="general",
            ),
            Tool(
                name="list_daemons",
                description="List running collector daemons with optional filters",
                parameters={
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "source": {"type": "string"},
                        "instance_pattern": {"type": "string"},
                        "min_priority": {"type": "integer"},
                    },
                },
                callable=tool_list_daemons,
                category="ops",
            ),
            Tool(
                name="cleanup_report",
                description="Get a retention cleanup report and daemon status",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 100},
                        "window_hours": {"type": "integer", "default": 24},
                    },
                },
                callable=tool_cleanup_report,
                category="ops",
            ),
        ]
        for tool in tools:
            self._tools[tool.name] = tool

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_all(self) -> list[Tool]:
        return list(self._tools.values())

    def by_category(self, category: str) -> list[Tool]:
        return [t for t in self._tools.values() if t.category == category]

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Return JSON schema representations of all tools."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in self._tools.values()
        ]
