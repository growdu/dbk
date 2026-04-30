"""
Sample DBK Agent plugins demonstrating the plugin system.

These plugins are loaded from dbk_plugins/samples/ via the directory plugin
discovery mechanism.  To use them, ensure this directory is in your
DBK_PLUGIN_DIR or ~/.dbk/plugins/samples/.

Plugins:
- prometheus_plugin  : exposes a /metrics endpoint (Prometheus-compatible)
- pgaudit_plugin    : registers a pgaudit_summary tool (needs pg_audit extension)
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from dbk.plugins import PluginABC, hookimpl


# ── Prometheus metrics plugin ─────────────────────────────────────────────────

class PrometheusPlugin(PluginABC):
    """Exposes a /metrics endpoint with Prometheus-compatible text format."""

    name = "prometheus_exporter"
    version = "1.0.0"

    def __init__(self) -> None:
        self._counter_requests = 0
        self._counter_errors = 0
        self._histogram_tokens: list[float] = []
        self._last_request_time = 0.0

    def dbk_tool_register(self, registry):
        # No extra tools needed for this plugin.
        pass

    def dbk_post_message(self, message: str, result: dict[str, Any]) -> None:
        self._counter_requests += 1
        if result.get("error"):
            self._counter_errors += 1
        content = result.get("content", "")
        if content:
            # Rough token estimate: ~4 chars per token
            self._histogram_tokens.append(len(content) / 4)

    def dbk_api_routes(self):
        def metrics_handler() -> dict[str, str]:
            lines = [
                "# HELP dbk_requests_total Total chat requests processed",
                "# TYPE dbk_requests_total counter",
                f"dbk_requests_total {self._counter_requests}",
                "# HELP dbk_requests_errors_total Requests that resulted in an error",
                "# TYPE dbk_requests_errors_total counter",
                f"dbk_requests_errors_total {self._counter_errors}",
                "# HELP dbk_response_tokens Estimated response tokens (approx)",
                "# TYPE dbk_response_tokens gauge",
            ]
            # Avg / max token count
            if self._histogram_tokens:
                avg = sum(self._histogram_tokens) / len(self._histogram_tokens)
                mx = max(self._histogram_tokens)
                lines.append(f"dbk_response_tokens_avg {avg:.1f}")
                lines.append(f"dbk_response_tokens_max {mx:.1f}")
            else:
                lines.append("dbk_response_tokens_avg 0")
                lines.append("dbk_response_tokens_max 0")
            return {"text/plain": "\n".join(lines)}

        return [
            ("/metrics", "GET", {"handler": metrics_handler}),
        ]


# ── pgAudit helper plugin ─────────────────────────────────────────────────────

class PgAuditPlugin(PluginABC):
    """Registers a pgaudit_summary tool (no-op without pg_audit extension)."""

    name = "pgaudit_helper"
    version = "1.0.0"

    def dbk_tool_register(self, registry):
        from dbk.agent.tools import Tool

        def tool_pgaudit_summary(instance: str = "pg-main-01", dsn: str | None = None) -> dict[str, Any]:
            """Summarize recent pg_audit log entries (requires pg_audit extension).

            Without pg_audit, this tool returns a helpful message.
            """
            # Try to query pg_audit if available
            resolved_dsn = dsn
            if not resolved_dsn:
                import os
                resolved_dsn = os.environ.get("DBK_PG_DSN", "")

            if not resolved_dsn:
                return {
                    "ok": True,
                    "result": {
                        "available": False,
                        "message": (
                            "pg_audit extension not available or DBK_PG_DSN not set. "
                            "Install pg_audit in PostgreSQL and set DBK_PG_DSN to enable "
                            "this tool."
                        ),
                    },
                }

            try:
                import psycopg
            except ImportError:
                return {
                    "ok": True,
                    "result": {
                        "available": False,
                        "message": "psycopg not installed. Cannot connect to PostgreSQL.",
                    },
                }

            try:
                conn = psycopg.connect(resolved_dsn, connect_timeout=5)
                cur = conn.cursor()
                cur.execute("""
                    SELECT recorded_at, class, command, object_type, object_name, user_name, statement_id
                    FROM pgaudit.log
                    WHERE recorded_at > NOW() - INTERVAL '1 hour'
                    ORDER BY recorded_at DESC
                    LIMIT 20
                """)
                rows = cur.fetchall()
                conn.close()
                return {
                    "ok": True,
                    "result": {
                        "available": True,
                        "count": len(rows),
                        "entries": [
                            {
                                "ts": str(r[0]),
                                "class": r[1],
                                "command": r[2],
                                "object_type": r[3],
                                "object_name": r[4],
                                "user": r[5],
                                "stmt_id": r[6],
                            }
                            for r in rows
                        ],
                    },
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": True,
                    "result": {
                        "available": False,
                        "error": str(exc),
                        "message": (
                            "Could not query pg_audit. Ensure the extension is installed "
                            "and the connection has permission to read pgaudit.log."
                        ),
                    },
                }

        registry.register(
            Tool(
                name="pgaudit_summary",
                description="Summarize recent pg_audit log entries (requires pg_audit extension)",
                parameters={
                    "type": "object",
                    "properties": {
                        "instance": {"type": "string", "default": "pg-main-01"},
                        "dsn": {"type": "string"},
                    },
                },
                callable=tool_pgaudit_summary,
                category="diagnose",
            )
        )


# ── Register sample plugins ───────────────────────────────────────────────────

prometheus_plugin = PrometheusPlugin()
pgaudit_plugin = PgAuditPlugin()
