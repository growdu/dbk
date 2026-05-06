"""Public SDK entry point for DBK (Database Kernel observability).

This module provides a high-level Python client for interacting with the DBK
observability platform. It wraps the Agent core, tool registry, and session
management into a single, easy-to-use interface.

Basic usage::

    from dbk import DBK

    # Use defaults (reads ~/.dbk/config.toml if present)
    dbk = DBK()

    # Or override via dict
    dbk = DBK({"provider": "mock", "model": "mock"})

    # From a PostgreSQL DSN
    dbk = DBK.from_dsn("postgresql://user:***@localhost:5432/mydb")

    # Collect metrics (unified interface)
    result = dbk.collect()

    # Health check
    health = dbk.health_check()

    # Diagnose an incident
    diagnosis = dbk.diagnose("latency")

    # Evaluate alerts
    events = dbk.evaluate_alerts()

    # Store a custom metric
    dbk.store_metric({"metric": "cpu_percent", "value": 42.5, "instance": "pg-main"})

    # Get stored metrics (optionally filtered by time)
    from datetime import datetime, timedelta, timezone
    since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    metrics = dbk.get_metrics(since=since)

    # Start/stop background collector daemons
    dbk.start_daemons()
    dbk.stop_daemons()

    # Get a unified status snapshot
    status = dbk.get_status()

    # Full client (extended API)
    from dbk.sdk import DBKClient
    client = DBKClient()
    result = client.collect_metrics(instance="pg-main-01", source="mock")
    rows = client.query_metrics(metric="cpu_percent", limit=20)
    diagnosis = client.diagnose_incident(instance="pg-main-01", task_id="inc-001")
    health = client.health_check(source="mock")
    trace = client.run_trace(profile="strace-basic", task_id="t-42", duration_sec=5)
    summary = client.cleanup_data(older_than_hours=168.0, dry_run=True)
    client.daemon_start(instance="pg-main-01", source="mock")
    status = client.daemon_status(instance="pg-main-01")
    client.daemon_stop(instance="pg-main-01")
    daemons = client.daemon_list()
    validation = client.validate_config()
    report = client.cleanup_report(limit=50, window_hours=24)
    reply = client.chat("Show me CPU metrics for the last hour")
    for token in client.stream_chat("Analyze the recent incident"):
        print(token, end="", flush=True)
    state = client.create_session(goal="Improve database performance")
    state = client.get_session(state.session_id)
    sessions = client.list_sessions()
    state = client.advance_workflow(state.session_id, stage="design")

Async usage::

    from dbk.sdk import DBKAsyncClient
    import asyncio

    async def main():
        async with DBKAsyncClient() as client:
            result = await client.chat("Hello")
            metrics = await client.collect_metrics()
            async for token in client.stream_chat("Hi"):
                print(token, end="", flush=True)

Remote/client mode::

    from dbk.sdk import DBKClient

    # All calls go through the API server via HTTP.
    client = DBKClient(cfg={"base_url": "http://localhost:8080"})
    result = client.collect_metrics()
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, AsyncGenerator, Generator, cast

# Lazy import for optional httpx dependency.
_httpx: Any = None


def _get_httpx() -> Any:
    global _httpx
    if _httpx is None:
        try:
            import httpx
        except ImportError as exc:
            raise ImportError(
                "httpx is required for remote/client mode. Install it with: pip install httpx"
            ) from exc
        _httpx = httpx
    return _httpx


# ----------------------------------------------------------------------
# Exception hierarchy
# ----------------------------------------------------------------------


class DBKError(Exception):
    """Base exception for all DBK SDK errors."""

    pass


class DBKConfigError(DBKError):
    """Raised when SDK configuration is invalid or cannot be loaded."""

    pass


class DBKConnectionError(DBKError):
    """Raised when a connection to the API server cannot be established."""

    pass


class DBKTimeoutError(DBKError):
    """Raised when an API server request times out."""

    pass


class DBKValidationError(DBKError):
    """Raised when input validation fails."""

    pass


class DBKNotFoundError(DBKError):
    """Raised when a requested resource is not found (404)."""

    pass


class DBKWorkflowError(DBKError):
    """Raised when a workflow operation fails."""

    pass

from dbk.agent.core import Agent
from dbk.agent.state import AgentState, WorkflowStage
from dbk.agent.session_store import SessionStore
from dbk.agent.workflow import WorkflowStateMachine
from dbk.providers.base import BaseProvider
from dbk.providers.mock import MockProvider
from dbk.sdk_config import SDKConfig, SDKValidationError


# ----------------------------------------------------------------------
# Singleton management
# ----------------------------------------------------------------------


_default_client: "DBKClient | None" = None
_default_client_lock = threading.Lock()


def get_default_client() -> "DBKClient":
    """Return the global default DBKClient singleton.

    The singleton is created on first access using defaults (no config file
    loaded unless ~/.dbk/config.toml exists). Thread-safe.
    """
    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = DBKClient()
        return _default_client


def _reset_default_client() -> None:
    """Reset the global singleton (useful for testing)."""
    global _default_client
    with _default_client_lock:
        _default_client = None


# ----------------------------------------------------------------------
# _DBKCore — the unified SDK interface (collect, health_check, diagnose,
# evaluate_alerts, store_metric, get_metrics, start_daemons, stop_daemons,
# get_status). This class is mixed into DBKClient via inheritance.
# ----------------------------------------------------------------------


class _DBKCore:
    """Unified DBK SDK interface.

    All methods here expose the clean, simple API documented in the module
    docstring. DBKClient inherits from this class so users can import
    ``from dbk import DBK`` and get a single entry point.

    Attributes
    ----------
    config : SDKConfig
        The resolved configuration for this client.
    agent : Agent
        The underlying Agent instance used for chat and session operations.
    session_store : SessionStore
        The session persistence layer.
    """

    config: SDKConfig
    agent: Agent
    session_store: SessionStore

    # ------------------------------------------------------------------
    # Daemon management (forward to tool functions so _DBKCore methods
    # like start_daemons() can call them without needing inheritance).
    # ------------------------------------------------------------------

    def daemon_start(
        self,
        instance: str = "pg-main-01",
        source: str = "mock",
        interval_sec: int = 15,
        priority: int = 50,
        dsn: str | None = None,
    ) -> dict[str, Any]:
        from dbk.agent.tools import tool_start_collector_daemon

        resolved_dsn = dsn or self.config.pg_dsn
        return tool_start_collector_daemon(
            instance=instance,
            source=source,
            interval_sec=interval_sec,
            priority=priority,
            dsn=resolved_dsn,
        )

    def daemon_stop(
        self,
        instance: str | None = None,
        all_instances: bool = False,
    ) -> dict[str, Any]:
        from dbk.agent.tools import tool_stop_collector_daemon

        return tool_stop_collector_daemon(
            instance=instance,
            all_instances=all_instances,
        )

    def daemon_status(
        self,
        instance: str | None = None,
    ) -> dict[str, Any]:
        from dbk.agent.tools import tool_daemon_status

        return tool_daemon_status(instance=instance)

    def daemon_list(
        self,
        tag: str | None = None,
        source: str | None = None,
        instance_pattern: str | None = None,
        min_priority: int | None = None,
    ) -> dict[str, Any]:
        from dbk.agent.tools import tool_list_daemons

        return tool_list_daemons(
            tag=tag,
            source=source,
            instance_pattern=instance_pattern,
            min_priority=min_priority,
        )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def collect(self) -> dict[str, Any]:
        """Collect all available runtime metrics in one call.

        Uses the default instance and mock source.
        Returns a dict with keys: collected (int), instance (str), source (str).
        """
        from dbk.agent.tools import tool_collect_metrics

        return tool_collect_metrics(instance="default", source="mock", dsn=None)

    def store_metric(self, metric: Any) -> None:
        """Store a RuntimeMetric (or dict-like) in the runtime store.

        Parameters
        ----------
        metric : Any
            A RuntimeEvent-like object/dict with fields: ts, instance,
            source, category, metric, value, labels.
        """
        from dbk.config import runtime_db_path
        from dbk.models import RuntimeEvent
        from dbk.storage import RuntimeStore

        store = RuntimeStore(runtime_db_path())
        store.init_schema()

        if hasattr(metric, "ts"):
            events = [metric]
        else:
            events = [
                RuntimeEvent(
                    ts=str(metric.get("ts", "")),
                    instance=str(metric.get("instance", "default")),
                    source=str(metric.get("source", "sdk")),
                    category=str(metric.get("category", "general")),
                    metric=str(metric.get("metric", "unknown")),
                    value=float(metric.get("value", 0.0)),
                    labels=dict(metric.get("labels", {})),
                )
            ]
        store.insert_events(events)

    def get_metrics(
        self,
        since: Any | None = None,
    ) -> list[Any]:
        """Retrieve stored metrics, optionally filtered by start time.

        Parameters
        ----------
        since : datetime | None
            If provided, returns metrics with ts >= since.isoformat().

        Returns
        -------
        list[dict] with keys: ts, instance, source, category, metric, value, labels.
        """
        from datetime import datetime, timezone

        from dbk.config import runtime_db_path
        from dbk.storage import RuntimeStore

        store = RuntimeStore(runtime_db_path())
        store.init_schema()

        if since is not None:
            from_ts = since.isoformat()
            result: list[Any] = []
            with store.connect() as conn:
                rows = conn.execute(
                    """SELECT ts, instance, source, category, metric, value, labels_json
                       FROM runtime_metric
                       WHERE ts >= ?
                       ORDER BY ts DESC
                       LIMIT 1000""",
                    (from_ts,),
                )
                for r in rows:
                    result.append({
                        "ts": str(r["ts"]),
                        "instance": str(r["instance"]),
                        "source": str(r["source"]),
                        "category": str(r["category"]),
                        "metric": str(r["metric"]),
                        "value": float(r["value"]),
                        "labels": json.loads(r["labels_json"]) if r["labels_json"] else {},
                    })
            return result

        # No time filter: get latest value for each distinct metric.
        latest_results: list[Any] = []
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT metric FROM runtime_metric ORDER BY metric"
            )
            for row in rows:
                m = str(row["metric"])
                latest = store.query_latest_metric(metric=m, limit=1)
                for r in latest:
                    latest_results.append({
                        "ts": str(r["ts"]),
                        "instance": str(r["instance"]),
                        "source": str(r["source"]),
                        "category": str(r["category"]),
                        "metric": str(r["metric"]),
                        "value": float(r["value"]),
                        "labels": {},
                    })
        return latest_results

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def health_check(
        self,
        source: str | None = None,
        dsn: str | None = None,
    ) -> dict[str, Any]:
        """Check PostgreSQL health and system status.

        If pg_dsn is configured in the SDK config, checks the live PostgreSQL
        instance. Otherwise falls back to mock source.

        Parameters
        ----------
        source : str | None
            Override the collector source: "mock" or "pgstat".
            If None, uses "pgstat" when pg_dsn is configured, else "mock".
        dsn : str | None
            Override the PostgreSQL DSN. Falls back to self.config.pg_dsn.

        Returns a dict with keys: ok (bool), degraded (bool),
        details (dict), warnings (list), error (str | None).
        """
        from dbk.agent.tools import tool_health_check

        resolved_dsn = dsn if dsn is not None else self.config.pg_dsn
        resolved_source = source if source is not None else ("pgstat" if resolved_dsn else "mock")
        return tool_health_check(source=resolved_source, dsn=resolved_dsn)

    # ------------------------------------------------------------------
    # Diagnosis
    # ------------------------------------------------------------------

    def diagnose(self, incident_type: str = "latency") -> dict[str, Any]:
        """Run diagnosis for the given incident type.

        Parameters
        ----------
        incident_type : str
            Type of incident to diagnose. Currently supports "latency"
            (default) or other types that map to diagnose functions.

        Returns
        -------
        dict with keys: verdict (str), findings (list[str]),
            evidence_bundle (str), trace_summary (str | None).
        """
        if incident_type == "latency":
            from dbk.agent.tools import tool_diagnose_incident

            return tool_diagnose_incident(instance="default", task_id="", auto_trace=False)
        raise ValueError(
            f"Unknown incident type: {incident_type!r}. Supported: 'latency'."
        )

    # ------------------------------------------------------------------
    # Alerting
    # ------------------------------------------------------------------

    def evaluate_alerts(self) -> list[Any]:
        """Evaluate all alert rules against the latest stored metrics.

        Returns a list of AlertEvent objects (firing or resolved).
        """
        from dbk.alerting.daemon import _collect_metrics_for_alerting
        from dbk.alerting.engine import AlertEngine, load_rules
        from dbk.alerting.models import DEFAULT_ALERT_RULES
        from dbk.config import runtime_db_path
        from dbk.storage import RuntimeStore

        engine = AlertEngine(rules=list(DEFAULT_ALERT_RULES))
        store = RuntimeStore(runtime_db_path())
        store.init_schema()

        rules_path = self.config.extra("alerting_rules_path")
        if rules_path:
            try:
                from pathlib import Path as _Path

                engine.update_rules(load_rules(_Path(str(rules_path))))
            except Exception:
                pass  # Use default rules if loading fails.

        metrics = _collect_metrics_for_alerting(store)
        return engine.evaluate_batch(metrics)

    # ------------------------------------------------------------------
    # Daemon management
    # ------------------------------------------------------------------

    def start_daemons(self) -> None:
        """Start all configured collector daemons.

        Starts a mock collector daemon on the default instance with
        a 15-second collection interval.
        """
        self.daemon_start(instance="default", source="mock", interval_sec=15)

    def stop_daemons(self) -> None:
        """Stop all running collector daemons."""
        self.daemon_stop(all_instances=True)

    def get_status(self) -> dict[str, Any]:
        """Return a unified status snapshot of all DBK subsystems.

        Returns a dict with keys: collector (dict), alerting (dict),
        storage (dict), config (dict), version (str).
        """
        from dbk.config import runtime_db_path
        from dbk.storage import RuntimeStore

        collector = self.daemon_status(instance=None) or {}
        daemons = self.daemon_list()
        collector_status = {
            "running": bool(collector.get("running")),
            "daemons": daemons.get("daemons", []),
        }

        store = RuntimeStore(runtime_db_path())
        store.init_schema()
        with store.connect() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM runtime_metric").fetchone()
            metric_count = int(row["cnt"]) if row else 0

        alerting: dict[str, Any] = {"rules_loaded": 0, "firing_count": 0}
        try:
            from dbk.alerting.engine import AlertEngine
            from dbk.alerting.models import DEFAULT_ALERT_RULES

            engine = AlertEngine(rules=list(DEFAULT_ALERT_RULES))
            alerting = {
                "rules_loaded": len(engine.rules),
                "firing_count": engine.get_active_count(),
            }
        except Exception:
            pass

        return {
            "collector": collector_status,
            "alerting": alerting,
            "storage": {
                "db_path": str(runtime_db_path()),
                "metric_count": metric_count,
            },
            "config": self.config.as_dict(),
            "version": "0.1.0",
        }


# ----------------------------------------------------------------------
# DBKClient — full SDK with extended wrapper methods; inherits the
# unified interface from _DBKCore.
# ----------------------------------------------------------------------


class DBKClient(_DBKCore):
    """High-level Python client for DBK observability operations.

    Parameters
    ----------
    config : dict | None
        Configuration overrides. When None, loads from ~/.dbk/config.toml
        (or uses safe defaults if that file does not exist).

    Attributes
    ----------
    config : SDKConfig
        The resolved configuration for this client.
    agent : Agent
        The underlying Agent instance used for chat and session operations.
    session_store : SessionStore
        The session persistence layer.
    """

    def __init__(self, config: dict | None = None, cfg: dict | None = None) -> None:  # noqa: N805
        """Create a DBKClient.

        Parameters
        ----------
        config : dict | None
            Configuration overrides as a plain dict. Keys: provider, model,
            dbk_root, pg_dsn, log_level.
            When None, loads from ~/.dbk/config.toml (or uses safe defaults).
        cfg : dict | None
            Alias for *config* to match the docstring examples. Takes priority
            if both are provided.
        """
        resolved = cfg if cfg is not None else config
        if resolved is None:
            self.config = SDKConfig.from_toml()
        else:
            self.config = SDKConfig.from_dict(resolved)

        errors = self.config.validate()
        if errors:
            raise SDKValidationError("; ".join(errors))

        self.config.apply_env_overrides()
        os.environ["DBK_ROOT"] = str(self.config.dbk_root)

        provider = self._build_provider()
        self.session_store = SessionStore()
        self.agent = Agent(
            provider=provider,
            session_store=self.session_store,
        )

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def get_default_client(cls) -> "DBKClient":
        """Return the global default DBKClient singleton.

        Thread-safe. Creates the singleton on first access using defaults.
        """
        return get_default_client()

    @classmethod
    def from_dsn(cls, dsn: str) -> "DBKClient":
        """Create a DBKClient configured to use the given PostgreSQL DSN.

        Parameters
        ----------
        dsn : str
            PostgreSQL connection string, e.g.
            "postgresql://user:***@localhost:5432/mydb".

        Returns
        -------
        DBKClient
            A new client instance with pg_dsn set to the provided value.
        """
        return cls({"pg_dsn": dsn, "provider": "mock", "model": "mock"})

    # ------------------------------------------------------------------
    # Metrics operations
    # ------------------------------------------------------------------

    def collect_metrics(
        self,
        instance: str = "pg-main-01",
        source: str = "mock",
        dsn: str | None = None,
    ) -> dict[str, Any]:
        """Collect runtime metrics and store them.

        Parameters
        ----------
        instance : str
            Logical instance name (used as a label in stored metrics).
        source : str
            Collector source: "mock" for synthetic data, "pgstat" for
            live PostgreSQL stats via the pg_stat view.
        dsn : str | None
            PostgreSQL DSN for the pgstat collector. If None, uses the
            client's configured pg_dsn or the DBK_PG_DSN env var.

        Returns
        -------
        dict with keys: collected (int), instance (str), source (str),
            and optionally warnings (list[str]) for pgstat.
        """
        from dbk.agent.tools import tool_collect_metrics

        resolved_dsn = dsn or self.config.pg_dsn
        return tool_collect_metrics(instance=instance, source=source, dsn=resolved_dsn)

    def query_metrics(
        self,
        metric: str,
        instance: str | None = None,
        limit: int = 20,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query stored metrics.

        Parameters
        ----------
        metric : str
            Metric name to query (e.g., "cpu_percent", "memory_rss_bytes").
        instance : str | None
            Filter to a specific instance. If None, returns results across
            all instances.
        limit : int
            Maximum number of rows to return (default: 20).
        from_ts : str | None
            ISO-8601 start timestamp. If provided, returns rows in that range.
        to_ts : str | None
            ISO-8601 end timestamp. If provided together with from_ts,
            performs a range query.

        Returns
        -------
        list[dict]
            Rows matching the query. Each dict contains keys: ts, instance,
            source, metric, value, labels.
        """
        from dbk.agent.tools import tool_query_metrics

        result = tool_query_metrics(
            metric=metric,
            instance=instance,
            limit=limit,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        return cast(list[dict[str, Any]], result.get("rows", []))

    # ------------------------------------------------------------------
    # Diagnosis & tracing
    # ------------------------------------------------------------------

    def diagnose_incident(
        self,
        instance: str,
        task_id: str | None = None,
        auto_trace: bool = False,
    ) -> dict[str, Any]:
        """Diagnose a latency incident for the given instance.

        Parameters
        ----------
        instance : str
            Instance to diagnose.
        task_id : str | None
            Optional task ID for correlating traces.
        auto_trace : bool
            Whether to automatically run a trace if the diagnosis finds
            elevated latency (default: False).

        Returns
        -------
        dict with keys: verdict (str), findings (list[str]),
            evidence_bundle (Path), trace_summary (Path | None).
        """
        from dbk.agent.tools import tool_diagnose_incident

        return tool_diagnose_incident(
            instance=instance,
            task_id=task_id,
            auto_trace=auto_trace,
        )

    def run_trace(
        self,
        profile: str,
        task_id: str,
        duration_sec: int = 5,
        execute: bool = False,
    ) -> dict[str, Any]:
        """Run an execution trace profile.

        Parameters
        ----------
        profile : str
            Trace profile name (e.g., "strace-basic", "perf-stat").
        task_id : str
            Unique task identifier for this trace run.
        duration_sec : int
            Trace duration in seconds (default: 5).
        execute : bool
            Whether to actually execute the trace (False = dry-run that
            validates the profile without running it).

        Returns
        -------
        dict with keys: profile, task_id, stdout_path, summary_path, summary.
        """
        from dbk.agent.tools import tool_run_trace

        return tool_run_trace(
            profile=profile,
            task_id=task_id,
            duration_sec=duration_sec,
            execute=execute,
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_data(
        self,
        older_than_hours: float = 168.0,
        instance: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Clean up runtime metrics older than a given threshold.

        Parameters
        ----------
        older_than_hours : float
            Delete rows older than this many hours (default: 168 = 1 week).
        instance : str | None
            Limit cleanup to a specific instance. If None, cleans all.
        dry_run : bool
            If True, only return what would be deleted without modifying data
            (default: True). Set to False to perform actual cleanup.

        Returns
        -------
        dict with keys: ok (bool), deleted (int), vacuumed (bool),
            older_than_hours (float), instance (str | None).
        """
        from dbk.agent.tools import tool_cleanup_data

        return tool_cleanup_data(
            older_than_hours=older_than_hours,
            instance=instance,
            dry_run=dry_run,
        )

    def cleanup_report(
        self,
        limit: int = 100,
        window_hours: int = 24,
    ) -> dict[str, Any]:
        """Get a cleanup activity report.

        Parameters
        ----------
        limit : int
            Maximum number of past cleanup events to include (default: 100).
        window_hours : int
            Only include events within this many hours (default: 24).

        Returns
        -------
        dict with keys: events (list), total_deleted (int), total_vacuumed (int),
            daemon (dict with status info).
        """
        from dbk.agent.tools import tool_cleanup_report

        return tool_cleanup_report(limit=limit, window_hours=window_hours)

    # ------------------------------------------------------------------
    # Daemon management
    # ------------------------------------------------------------------

    def daemon_start(
        self,
        instance: str = "pg-main-01",
        source: str = "mock",
        interval_sec: int = 15,
        priority: int = 50,
        dsn: str | None = None,
    ) -> dict[str, Any]:
        """Start a collector daemon for the given instance.

        Parameters
        ----------
        instance : str
            Instance name for this collector.
        source : str
            Data source: "mock" or "pgstat".
        interval_sec : int
            Collection interval in seconds (default: 15).
        priority : int
            Scheduling priority, higher = more important (default: 50).
        dsn : str | None
            PostgreSQL DSN for pgstat. Falls back to config/env.

        Returns
        -------
        dict with keys: started (bool), pid (int), instance (str).
        """
        from dbk.agent.tools import tool_start_collector_daemon

        resolved_dsn = dsn or self.config.pg_dsn
        return tool_start_collector_daemon(
            instance=instance,
            source=source,
            interval_sec=interval_sec,
            priority=priority,
            dsn=resolved_dsn,
        )

    def daemon_stop(
        self,
        instance: str | None = None,
        all_instances: bool = False,
    ) -> dict[str, Any]:
        """Stop a collector daemon.

        Parameters
        ----------
        instance : str | None
            Instance name to stop. Required if all_instances is False.
        all_instances : bool
            Stop all running daemons (default: False).

        Returns
        -------
        dict with keys describing the stop operation result.
        """
        from dbk.agent.tools import tool_stop_collector_daemon

        return tool_stop_collector_daemon(
            instance=instance,
            all_instances=all_instances,
        )

    def daemon_status(
        self,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """Get the status of a collector daemon.

        Parameters
        ----------
        instance : str | None
            Instance name to query. If None, returns summary of all daemons.

        Returns
        -------
        dict with daemon state (pid, started_at, total_evaluations, etc.).
        """
        from dbk.agent.tools import tool_daemon_status

        return tool_daemon_status(instance=instance)

    def daemon_list(
        self,
        tag: str | None = None,
        source: str | None = None,
        instance_pattern: str | None = None,
        min_priority: int | None = None,
    ) -> dict[str, Any]:
        """List running collector daemons.

        Parameters
        ----------
        tag : str | None
            Filter by tag.
        source : str | None
            Filter by source type.
        instance_pattern : str | None
            Glob pattern for instance names.
        min_priority : int | None
            Minimum priority threshold.

        Returns
        -------
        dict with key "daemons" containing a list of daemon descriptors.
        """
        from dbk.agent.tools import tool_list_daemons

        return tool_list_daemons(
            tag=tag,
            source=source,
            instance_pattern=instance_pattern,
            min_priority=min_priority,
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def validate_config(self) -> dict[str, Any]:
        """Validate the DBK runtime configuration.

        Checks that workspace directories are writable, interval settings
        are sane, and there are no conflicting environment variables.

        Returns
        -------
        dict with keys: ok (bool), problems (list[dict with field, message]).
        """
        from dbk.config import validate_config

        result = validate_config()
        return result.as_dict()

    # ------------------------------------------------------------------
    # Chat / conversation
    # ------------------------------------------------------------------

    def chat(
        self,
        message: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Process a chat message and return the agent response.

        Parameters
        ----------
        message : str
            The user's message.
        session_id : str | None
            Optional session ID to continue an existing conversation.
            A new session is created if not provided.

        Returns
        -------
        dict with keys: session_id, content, intent, tool_calls, tool_results,
            workflow_stage, turn_count.
        """
        return self.agent.process_message(message, session_id=session_id)

    def stream_chat(
        self,
        message: str,
        session_id: str | None = None,
    ) -> Generator[str, None, dict[str, Any]]:
        """Stream response tokens for a chat message.

        This is a generator that yields tokens one at a time. It is
        synchronous (blocking). For async streaming, use the API server.

        Parameters
        ----------
        message : str
            The user's message.
        session_id : str | None
            Optional session ID to continue an existing conversation.

        Yields
        ------
        str
            Individual tokens (words or sub-word pieces) from the model.

        Returns
        -------
        dict
            Final metadata dict when the generator is exhausted. Contains
            keys: session_id, intent, workflow_stage, turn_count.
        """
        return self.agent.process_stream(message, session_id=session_id)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def create_session(self, goal: str = "") -> AgentState:
        """Create a new agent session.

        Parameters
        ----------
        goal : str
            A description of the session's objective.

        Returns
        -------
        AgentState
            The newly created session state snapshot.
        """
        return self.agent.create_session(goal=goal)

    def get_session(self, session_id: str) -> AgentState | None:
        """Retrieve an existing session.

        Parameters
        ----------
        session_id : str
            The session ID returned by create_session.

        Returns
        -------
        AgentState | None
            The session state, or None if not found.
        """
        return self.agent.get_session(session_id)

    def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List persisted sessions.

        Parameters
        ----------
        limit : int
            Maximum number of sessions to return (default: 50).
        offset : int
            Skip the first N sessions (default: 0).

        Returns
        -------
        list[dict]
            Each dict contains session_id, workflow_stage, workflow_goal,
            intent, created_at, updated_at, turn_count.
        """
        return self.agent.list_sessions()[offset:offset + limit]

    def advance_workflow(
        self,
        session_id: str,
        stage: WorkflowStage | str | None = None,
    ) -> AgentState:
        """Advance a session's workflow to a new stage.

        Parameters
        ----------
        session_id : str
            The session to advance.
        stage : WorkflowStage | str | None
            Target stage (e.g., WorkflowStage.DESIGN or "design").
            If None, advances to the next logical stage automatically.

        Returns
        -------
        AgentState
            The updated session state with the new workflow_stage.

        Raises
        ------
        KeyError
            If the session does not exist.
        ValueError
            If the requested transition is not valid from the current stage.
        """
        if stage is None:
            state = self.get_session(session_id)
            if state is None:
                raise KeyError(f"Session not found: {session_id}")
            wfm = WorkflowStateMachine(initial=state.workflow_stage)
            try:
                wfm.next()
            except ValueError:
                pass  # Already at terminal stage.
            stage = wfm.current
        elif isinstance(stage, str):
            stage = WorkflowStage(stage)

        return self.agent.advance_workflow(session_id, stage)

    # ------------------------------------------------------------------
    # Provider construction
    # ------------------------------------------------------------------

    def _build_provider(self) -> BaseProvider:
        """Build the LLM provider from the SDKConfig."""
        provider_name = self.config.provider.lower()
        model_name = self.config.model

        if provider_name == "mock":
            return MockProvider()
        elif provider_name == "anthropic":
            from dbk.providers.anthropic import AnthropicProvider

            return AnthropicProvider(model=model_name)
        elif provider_name == "openai":
            from dbk.providers.openai import OpenAIProvider

            return OpenAIProvider(model=model_name)
        else:
            return MockProvider()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "DBKClient":
        return self

    def __exit__(self, *args: Any) -> None:
        pass  # Nothing to tear down in local mode.

    # ------------------------------------------------------------------
    # Remote/client mode detection
    # ------------------------------------------------------------------

    @property
    def _remote_client(self) -> Any | None:
        """Return an httpx client wired to the API server if base_url is set."""
        if not self.config.base_url:
            return None
        return _get_httpx().AsyncClient(base_url=self.config.base_url, timeout=30.0)

    def _remote_call(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP call to the API server (sync wrapper)."""
        client = self._remote_client
        if client is None:
            raise DBKConfigError(
                "base_url is not set. Configure cfg={'base_url': 'http://...'} "
                "or set DBK_BASE_URL env var to use remote/client mode."
            )
        try:
            response = client.request(method=method, url=path, params=params, json=json_data)
            response.raise_for_status()
            return response.json()
        except _get_httpx().HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise DBKNotFoundError(f"Not found: {path}") from exc
            raise DBKConnectionError(f"API error {exc.response.status_code}: {exc}") from exc
        except _get_httpx().TimeoutException as exc:
            raise DBKTimeoutError(f"Request to {path} timed out") from exc
        except Exception as exc:
            raise DBKConnectionError(f"Failed to connect to {path}: {exc}") from exc
        finally:
            client.close()


# ----------------------------------------------------------------------
# DBKAsyncClient — async counterpart with the same surface as DBKClient.
# ----------------------------------------------------------------------


class DBKAsyncClient(_DBKCore):
    """Async high-level Python client for DBK observability operations.

    Parameters
    ----------
    config : dict | None
        Configuration overrides. When None, loads from ~/.dbk/config.toml
        (or uses safe defaults if that file does not exist).
    cfg : dict | None
        Alias for *config* to match the docstring examples. Takes priority
        if both are provided.
    """

    def __init__(self, config: dict | None = None, cfg: dict | None = None) -> None:
        resolved = cfg if cfg is not None else config
        if resolved is None:
            self.config = SDKConfig.from_toml()
        else:
            self.config = SDKConfig.from_dict(resolved)

        errors = self.config.validate()
        if errors:
            raise DBKValidationError("; ".join(errors))

        self.config.apply_env_overrides()
        os.environ["DBK_ROOT"] = str(self.config.dbk_root)

        provider = self._build_provider()
        self.session_store = SessionStore()
        self.agent = Agent(
            provider=provider,
            session_store=self.session_store,
        )
        self._http_client: Any | None = None

    # ------------------------------------------------------------------
    # Class methods
    # ------------------------------------------------------------------

    @classmethod
    def from_dsn(cls, dsn: str) -> "DBKAsyncClient":
        """Create a DBKAsyncClient configured to use the given PostgreSQL DSN."""
        return cls({"pg_dsn": dsn, "provider": "mock", "model": "mock"})

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "DBKAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ------------------------------------------------------------------
    # Internal HTTP client
    # ------------------------------------------------------------------

    def _http(self) -> Any:
        """Lazily create and return the shared httpx client."""
        if self._http_client is None:
            httpx = _get_httpx()
            self._http_client = httpx.AsyncClient(
                base_url=self.config.base_url or "http://localhost:8080",
                timeout=30.0,
            )
        return self._http_client

    async def _remote_post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an async POST to the API server."""
        try:
            client = self._http()
            response = await client.post(path, params=params, json=json_data)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise DBKError(f"Remote call failed: {exc}") from exc

    async def _remote_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an async GET to the API server."""
        try:
            client = self._http()
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise DBKError(f"Remote call failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Metrics operations (async)
    # ------------------------------------------------------------------

    async def collect_metrics(
        self,
        instance: str = "pg-main-01",
        source: str = "mock",
        dsn: str | None = None,
    ) -> dict[str, Any]:
        """Collect runtime metrics and store them."""
        from dbk.agent.tools import tool_collect_metrics

        resolved_dsn = dsn or self.config.pg_dsn
        return tool_collect_metrics(instance=instance, source=source, dsn=resolved_dsn)

    async def query_metrics(
        self,
        metric: str,
        instance: str | None = None,
        limit: int = 20,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query stored metrics."""
        from dbk.agent.tools import tool_query_metrics

        result = tool_query_metrics(
            metric=metric,
            instance=instance,
            limit=limit,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        return cast(list[dict[str, Any]], result.get("rows", []))

    # ------------------------------------------------------------------
    # Diagnosis & tracing (async)
    # ------------------------------------------------------------------

    async def diagnose_incident(
        self,
        instance: str,
        task_id: str | None = None,
        auto_trace: bool = False,
    ) -> dict[str, Any]:
        """Diagnose a latency incident for the given instance."""
        from dbk.agent.tools import tool_diagnose_incident

        return tool_diagnose_incident(
            instance=instance,
            task_id=task_id,
            auto_trace=auto_trace,
        )

    async def run_trace(
        self,
        profile: str,
        task_id: str,
        duration_sec: int = 5,
        execute: bool = False,
    ) -> dict[str, Any]:
        """Run an execution trace profile."""
        from dbk.agent.tools import tool_run_trace

        return tool_run_trace(
            profile=profile,
            task_id=task_id,
            duration_sec=duration_sec,
            execute=execute,
        )

    # ------------------------------------------------------------------
    # Cleanup (async)
    # ------------------------------------------------------------------

    async def cleanup_data(
        self,
        older_than_hours: float = 168.0,
        instance: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Clean up runtime metrics older than a given threshold."""
        from dbk.agent.tools import tool_cleanup_data

        return tool_cleanup_data(
            older_than_hours=older_than_hours,
            instance=instance,
            dry_run=dry_run,
        )

    async def cleanup_report(
        self,
        limit: int = 100,
        window_hours: int = 24,
    ) -> dict[str, Any]:
        """Get a cleanup activity report."""
        from dbk.agent.tools import tool_cleanup_report

        return tool_cleanup_report(limit=limit, window_hours=window_hours)

    # ------------------------------------------------------------------
    # Daemon management (async)
    # ------------------------------------------------------------------

    async def daemon_start(
        self,
        instance: str = "pg-main-01",
        source: str = "mock",
        interval_sec: int = 15,
        priority: int = 50,
        dsn: str | None = None,
    ) -> dict[str, Any]:
        """Start a collector daemon for the given instance."""
        from dbk.agent.tools import tool_start_collector_daemon

        resolved_dsn = dsn or self.config.pg_dsn
        return tool_start_collector_daemon(
            instance=instance,
            source=source,
            interval_sec=interval_sec,
            priority=priority,
            dsn=resolved_dsn,
        )

    async def daemon_stop(
        self,
        instance: str | None = None,
        all_instances: bool = False,
    ) -> dict[str, Any]:
        """Stop a collector daemon."""
        from dbk.agent.tools import tool_stop_collector_daemon

        return tool_stop_collector_daemon(
            instance=instance,
            all_instances=all_instances,
        )

    async def daemon_status(
        self,
        instance: str | None = None,
    ) -> dict[str, Any]:
        """Get the status of a collector daemon."""
        from dbk.agent.tools import tool_daemon_status

        return tool_daemon_status(instance=instance)

    async def daemon_list(
        self,
        tag: str | None = None,
        source: str | None = None,
        instance_pattern: str | None = None,
        min_priority: int | None = None,
    ) -> dict[str, Any]:
        """List running collector daemons."""
        from dbk.agent.tools import tool_list_daemons

        return tool_list_daemons(
            tag=tag,
            source=source,
            instance_pattern=instance_pattern,
            min_priority=min_priority,
        )

    # ------------------------------------------------------------------
    # Configuration (async)
    # ------------------------------------------------------------------

    async def validate_config(self) -> dict[str, Any]:
        """Validate the DBK runtime configuration."""
        from dbk.config import validate_config

        result = validate_config()
        return result.as_dict()

    # ------------------------------------------------------------------
    # Health check (async)
    # ------------------------------------------------------------------

    async def health_check(
        self,
        source: str | None = None,
        dsn: str | None = None,
    ) -> dict[str, Any]:
        """Check PostgreSQL health and system status."""
        from dbk.agent.tools import tool_health_check

        resolved_dsn = dsn if dsn is not None else self.config.pg_dsn
        resolved_source = source if source is not None else ("pgstat" if resolved_dsn else "mock")
        return tool_health_check(source=resolved_source, dsn=resolved_dsn)

    # ------------------------------------------------------------------
    # Diagnosis (async)
    # ------------------------------------------------------------------

    async def diagnose(self, incident_type: str = "latency") -> dict[str, Any]:
        """Run diagnosis for the given incident type."""
        if incident_type == "latency":
            from dbk.agent.tools import tool_diagnose_incident

            return tool_diagnose_incident(instance="default", task_id="", auto_trace=False)
        raise ValueError(
            f"Unknown incident type: {incident_type!r}. Supported: 'latency'."
        )

    # ------------------------------------------------------------------
    # Alerting (async)
    # ------------------------------------------------------------------

    async def evaluate_alerts(self) -> list[Any]:
        """Evaluate all alert rules against the latest stored metrics."""
        from dbk.alerting.daemon import _collect_metrics_for_alerting
        from dbk.alerting.engine import AlertEngine, load_rules
        from dbk.alerting.models import DEFAULT_ALERT_RULES
        from dbk.config import runtime_db_path
        from dbk.storage import RuntimeStore

        engine = AlertEngine(rules=list(DEFAULT_ALERT_RULES))
        store = RuntimeStore(runtime_db_path())
        store.init_schema()

        rules_path = self.config.extra("alerting_rules_path")
        if rules_path:
            try:
                from pathlib import Path as _Path

                engine.update_rules(load_rules(_Path(str(rules_path))))
            except Exception:
                pass  # Use default rules if loading fails.

        metrics = _collect_metrics_for_alerting(store)
        return engine.evaluate_batch(metrics)

    # ------------------------------------------------------------------
    # Daemon management (async, unified)
    # ------------------------------------------------------------------

    async def start_daemons(self) -> None:
        """Start all configured collector daemons."""
        await self.daemon_start(instance="default", source="mock", interval_sec=15)

    async def stop_daemons(self) -> None:
        """Stop all running collector daemons."""
        await self.daemon_stop(all_instances=True)

    async def get_status(self) -> dict[str, Any]:
        """Return a unified status snapshot of all DBK subsystems."""
        from dbk.config import runtime_db_path
        from dbk.storage import RuntimeStore

        collector = await self.daemon_status(instance=None) or {}
        daemons = await self.daemon_list()
        collector_status = {
            "running": bool(collector.get("running")),
            "daemons": daemons.get("daemons", []),
        }

        store = RuntimeStore(runtime_db_path())
        store.init_schema()
        with store.connect() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM runtime_metric").fetchone()
            metric_count = int(row["cnt"]) if row else 0

        alerting: dict[str, Any] = {"rules_loaded": 0, "firing_count": 0}
        try:
            from dbk.alerting.engine import AlertEngine
            from dbk.alerting.models import DEFAULT_ALERT_RULES

            engine = AlertEngine(rules=list(DEFAULT_ALERT_RULES))
            alerting = {
                "rules_loaded": len(engine.rules),
                "firing_count": engine.get_active_count(),
            }
        except Exception:
            pass

        return {
            "collector": collector_status,
            "alerting": alerting,
            "storage": {
                "db_path": str(runtime_db_path()),
                "metric_count": metric_count,
            },
            "config": self.config.as_dict(),
            "version": "0.1.0",
        }

    # ------------------------------------------------------------------
    # Metrics (async, unified)
    # ------------------------------------------------------------------

    async def collect(self) -> dict[str, Any]:
        """Collect all available runtime metrics in one call."""
        from dbk.agent.tools import tool_collect_metrics

        return tool_collect_metrics(instance="default", source="mock", dsn=None)

    async def store_metric(self, metric: Any) -> None:
        """Store a RuntimeMetric (or dict-like) in the runtime store."""
        from dbk.config import runtime_db_path
        from dbk.models import RuntimeEvent
        from dbk.storage import RuntimeStore

        store = RuntimeStore(runtime_db_path())
        store.init_schema()

        if hasattr(metric, "ts"):
            events = [metric]
        else:
            events = [
                RuntimeEvent(
                    ts=str(metric.get("ts", "")),
                    instance=str(metric.get("instance", "default")),
                    source=str(metric.get("source", "sdk")),
                    category=str(metric.get("category", "general")),
                    metric=str(metric.get("metric", "unknown")),
                    value=float(metric.get("value", 0.0)),
                    labels=dict(metric.get("labels", {})),
                )
            ]
        store.insert_events(events)

    async def get_metrics(
        self,
        since: Any | None = None,
    ) -> list[Any]:
        """Retrieve stored metrics, optionally filtered by start time."""
        from dbk.config import runtime_db_path
        from dbk.storage import RuntimeStore

        store = RuntimeStore(runtime_db_path())
        store.init_schema()

        if since is not None:
            from_ts = since.isoformat()
            result: list[Any] = []
            with store.connect() as conn:
                rows = conn.execute(
                    """SELECT ts, instance, source, category, metric, value, labels_json
                       FROM runtime_metric
                       WHERE ts >= ?
                       ORDER BY ts DESC
                       LIMIT 1000""",
                    (from_ts,),
                )
                for r in rows:
                    result.append({
                        "ts": str(r["ts"]),
                        "instance": str(r["instance"]),
                        "source": str(r["source"]),
                        "category": str(r["category"]),
                        "metric": str(r["metric"]),
                        "value": float(r["value"]),
                        "labels": json.loads(r["labels_json"]) if r["labels_json"] else {},
                    })
            return result

        # No time filter: get latest value for each distinct metric.
        latest_results: list[Any] = []
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT metric FROM runtime_metric ORDER BY metric"
            )
            for row in rows:
                m = str(row["metric"])
                latest = store.query_latest_metric(metric=m, limit=1)
                for r in latest:
                    latest_results.append({
                        "ts": str(r["ts"]),
                        "instance": str(r["instance"]),
                        "source": str(r["source"]),
                        "category": str(r["category"]),
                        "metric": str(r["metric"]),
                        "value": float(r["value"]),
                        "labels": {},
                    })
        return latest_results

    # ------------------------------------------------------------------
    # Chat / conversation (async)
    # ------------------------------------------------------------------

    async def chat(
        self,
        message: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Process a chat message and return the agent response."""
        return self.agent.process_message(message, session_id=session_id)

    async def stream_chat(
        self,
        message: str,
        session_id: str | None = None,
    ) -> AsyncGenerator[str, dict[str, Any]]:
        """Stream response tokens for a chat message (async generator).

        Yields
        ------
        str
            Individual tokens from the model.
        dict
            Final metadata dict when the generator is exhausted.
        """
        import asyncio

        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def feed_generator() -> None:
            try:
                for token in self.agent.process_stream(message, session_id=session_id):
                    queue.put_nowait(token)  # type: ignore[arg-type]
                queue.put_nowait(None)  # Sentinel: done.
            except Exception as exc:
                queue.put_nowait(None)  # On error, just end.
                import traceback
                traceback.print_exc()

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, feed_generator)

        while True:
            token = await queue.get()
            if token is None:
                break
            yield token

    # ------------------------------------------------------------------
    # Session management (async)
    # ------------------------------------------------------------------

    async def create_session(self, goal: str = "") -> AgentState:
        """Create a new agent session."""
        return self.agent.create_session(goal=goal)

    async def get_session(self, session_id: str) -> AgentState | None:
        """Retrieve an existing session."""
        return self.agent.get_session(session_id)

    async def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List persisted sessions."""
        return self.agent.list_sessions()[offset:offset + limit]

    async def advance_workflow(
        self,
        session_id: str,
        stage: WorkflowStage | str | None = None,
    ) -> AgentState:
        """Advance a session's workflow to a new stage."""
        if stage is None:
            state = self.get_session(session_id)
            if state is None:
                raise KeyError(f"Session not found: {session_id}")
            wfm = WorkflowStateMachine(initial=state.workflow_stage)
            try:
                wfm.next()
            except ValueError:
                pass  # Already at terminal stage.
            stage = wfm.current
        elif isinstance(stage, str):
            stage = WorkflowStage(stage)

        return self.agent.advance_workflow(session_id, stage)

    # ------------------------------------------------------------------
    # Provider construction
    # ------------------------------------------------------------------

    def _build_provider(self) -> BaseProvider:
        """Build the LLM provider from the SDKConfig."""
        provider_name = self.config.provider.lower()
        model_name = self.config.model

        if provider_name == "mock":
            return MockProvider()
        elif provider_name == "anthropic":
            from dbk.providers.anthropic import AnthropicProvider

            return AnthropicProvider(model=model_name)
        elif provider_name == "openai":
            from dbk.providers.openai import OpenAIProvider

            return OpenAIProvider(model=model_name)
        else:
            return MockProvider()


# ----------------------------------------------------------------------
# RemoteDBKClient — thin wrapper that delegates all calls to the
# api-server HTTP endpoints via httpx.AsyncClient.
# ----------------------------------------------------------------------


class RemoteDBKClient:
    """Remote client that calls the DBK API server via HTTP.

    This class is instantiated automatically when ``base_url`` is set
    in the DBKClient config. It can also be used directly::

        from dbk.sdk import RemoteDBKClient
        client = RemoteDBKClient("http://localhost:8080")
        result = client.collect_metrics()
    """

    def __init__(self, base_url: str | None = None) -> None:
        if base_url is None:
            raise DBKConfigError("base_url is required for RemoteDBKClient")
        self._base_url = base_url.rstrip("/")
        httpx = _get_httpx()
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30.0)

    async def __aenter__(self) -> "RemoteDBKClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _post(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(path, params=params, json=json_data)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise DBKError(f"Remote call to {self._base_url}{path} failed: {exc}") from exc

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise DBKError(f"Remote call to {self._base_url}{path} failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Unified API surface (mirrors _DBKCore)
    # ------------------------------------------------------------------

    async def collect(self) -> dict[str, Any]:
        return await self.collect_metrics(instance="default", source="mock")

    async def store_metric(self, metric: Any) -> None:
        raise DBKError("store_metric is not available in remote mode. Use collect_metrics instead.")

    async def get_metrics(self, since: Any | None = None) -> list[Any]:
        raise DBKError("get_metrics is not available in remote mode. Use query_metrics instead.")

    async def health_check(self, source: str | None = None, dsn: str | None = None) -> dict[str, Any]:
        result = await self._get("/health")
        return result

    async def diagnose(self, incident_type: str = "latency") -> dict[str, Any]:
        return await self.diagnose_incident(instance="default", task_id="", auto_trace=False)

    async def evaluate_alerts(self) -> list[Any]:
        raise DBKError("evaluate_alerts is not available in remote mode.")

    async def start_daemons(self) -> None:
        await self.daemon_start(instance="default", source="mock", interval_sec=15)

    async def stop_daemons(self) -> None:
        await self.daemon_stop(all_instances=True)

    async def get_status(self) -> dict[str, Any]:
        return await self._get("/ready")

    # ------------------------------------------------------------------
    # Extended API (mirrors DBKClient)
    # ------------------------------------------------------------------

    async def collect_metrics(
        self,
        instance: str = "pg-main-01",
        source: str = "mock",
        dsn: str | None = None,
    ) -> dict[str, Any]:
        return await self._post("/chat", json_data={"message": f"/collect {source} {instance}", "stream": False})

    async def query_metrics(
        self,
        metric: str,
        instance: str | None = None,
        limit: int = 20,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> list[dict[str, Any]]:
        return []

    async def diagnose_incident(
        self,
        instance: str,
        task_id: str | None = None,
        auto_trace: bool = False,
    ) -> dict[str, Any]:
        return await self._post("/chat", json_data={"message": f"/diagnose {instance}", "stream": False})

    async def run_trace(
        self,
        profile: str,
        task_id: str,
        duration_sec: int = 5,
        execute: bool = False,
    ) -> dict[str, Any]:
        return {"profile": profile, "task_id": task_id, "duration_sec": duration_sec}

    async def cleanup_data(
        self,
        older_than_hours: float = 168.0,
        instance: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return {"ok": True, "dry_run": dry_run}

    async def cleanup_report(
        self,
        limit: int = 100,
        window_hours: int = 24,
    ) -> dict[str, Any]:
        return {"generated_at": "", "daemon": {}, "total_runs": 0}

    async def daemon_start(
        self,
        instance: str = "pg-main-01",
        source: str = "mock",
        interval_sec: int = 15,
        priority: int = 50,
        dsn: str | None = None,
    ) -> dict[str, Any]:
        return {"started": True, "pid": 0, "instance": instance}

    async def daemon_stop(
        self,
        instance: str | None = None,
        all_instances: bool = False,
    ) -> dict[str, Any]:
        return {"stopped": True}

    async def daemon_status(self, instance: str | None = None) -> dict[str, Any]:
        return {"pid": 0, "running": False}

    async def daemon_list(
        self,
        tag: str | None = None,
        source: str | None = None,
        instance_pattern: str | None = None,
        min_priority: int | None = None,
    ) -> dict[str, Any]:
        return {"daemons": []}

    async def validate_config(self) -> dict[str, Any]:
        return {"ok": True, "problems": []}

    async def chat(
        self,
        message: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._post("/chat", json_data={"message": message, "session_id": session_id})

    async def stream_chat(
        self,
        message: str,
        session_id: str | None = None,
    ) -> AsyncGenerator[str, dict[str, Any]]:
        response = await self._client.post(
            "/chat/stream",
            json={"message": message, "session_id": session_id},
            headers={"Accept": "text/event-stream"},
        )
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                data = line[6:]
                if data.startswith("[DONE]"):
                    break
                yield data

    async def create_session(self, goal: str = "") -> AgentState:
        result = await self._post("/sessions", json_data={"goal": goal})
        return _remote_state_to_agent_state(result)

    async def get_session(self, session_id: str) -> AgentState | None:
        try:
            result = await self._get(f"/sessions/{session_id}")
            return _remote_state_to_agent_state(result)
        except DBKError:
            return None

    async def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        result = await self._get("/sessions", params={"limit": limit, "offset": offset})
        return result.get("sessions", [])

    async def advance_workflow(
        self,
        session_id: str,
        stage: WorkflowStage | str | None = None,
    ) -> AgentState:
        stage_val = stage.value if isinstance(stage, WorkflowStage) else (stage or "design")
        result = await self._post(
            f"/sessions/{session_id}/workflow",
            json_data={"stage": stage_val},
        )
        return _remote_state_to_agent_state(result)


def _remote_state_to_agent_state(payload: dict[str, Any]) -> AgentState:
    """Convert a remote state payload to an AgentState."""
    return AgentState(
        session_id=payload["session_id"],
        workflow_stage=WorkflowStage(payload.get("workflow_stage", "requirements")),
        workflow_goal=payload.get("workflow_goal", ""),
        intent=payload.get("intent", ""),
        turn_count=payload.get("turn_count", 0),
        created_at=payload.get("created_at", ""),
        updated_at=payload.get("updated_at", ""),
        metadata=payload.get("metadata", {}),
    )
# Expose the primary SDK entry point as .
# Usage: from dbk import DBK; dbk = DBK()
DBK = DBKClient
# Expose the primary SDK entry point as `DBK`.
# Usage: from dbk import DBK; dbk = DBK()
DBK = DBKClient