from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import RuntimeEvent

try:
    import psycopg  # type: ignore
except Exception:  # pragma: no cover - tested by behavior, not import internals
    psycopg = None


class PgCollectorError(RuntimeError):
    """Raised when PostgreSQL collector cannot run."""


@dataclass(slots=True)
class PgCollectResult:
    events: list[RuntimeEvent]
    warnings: list[str]


@dataclass(slots=True)
class PgCapabilities:
    server_version: str
    server_version_num: int
    has_pg_stat_statements: bool
    has_pg_stat_io: bool


@dataclass(slots=True)
class PgHealthReport:
    ok: bool
    degraded: bool
    details: dict[str, Any]
    warnings: list[str]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "degraded": self.degraded,
            "details": self.details,
            "warnings": self.warnings,
            "error": self.error,
        }


def _build_runtime_events_from_values(instance: str, values: dict[str, float]) -> list[RuntimeEvent]:
    return [
        RuntimeEvent.create(
            instance=instance,
            source="pg_stat_statements",
            category="query",
            metric="query.p95_latency_ms",
            value=values["query.p95_latency_ms"],
            labels={"unit": "ms", "source": "pg"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="pg_stat_activity",
            category="wait",
            metric="wait.lock_ratio_pct",
            value=values["wait.lock_ratio_pct"],
            labels={"unit": "percent", "source": "pg"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="pg_stat_io",
            category="io",
            metric="io.read_latency_ms",
            value=values["io.read_latency_ms"],
            labels={"unit": "ms", "source": "pg"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="pg_locks",
            category="lock",
            metric="lock.blocked_sessions",
            value=values["lock.blocked_sessions"],
            labels={"unit": "count", "source": "pg"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="pg_stat_replication",
            category="replication",
            metric="replication.lag_sec",
            value=values["replication.lag_sec"],
            labels={"unit": "seconds", "source": "pg"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="pg_stat_database",
            category="buffer",
            metric="buffer.hit_ratio_pct",
            value=values["buffer.hit_ratio_pct"],
            labels={"unit": "percent", "source": "pg"},
        ),
    ]


def _query_single_float(cur: Any, query: str) -> float:
    cur.execute(query)
    row = cur.fetchone()
    raw = row[0] if row else 0.0
    return float(raw or 0.0)


def _detect_capabilities(cur: Any) -> PgCapabilities:
    cur.execute(
        """
        SELECT
          current_setting('server_version')::text AS server_version,
          current_setting('server_version_num')::int AS server_version_num
        """
    )
    row = cur.fetchone()
    server_version = str(row[0])
    server_version_num = int(row[1])

    has_pg_stat_statements = bool(
        _query_single_float(
            cur,
            """
            SELECT CASE WHEN EXISTS (
              SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'
            ) THEN 1 ELSE 0 END::float
            """,
        )
    )
    has_pg_stat_io = bool(
        _query_single_float(
            cur,
            """
            SELECT CASE WHEN to_regclass('pg_catalog.pg_stat_io') IS NOT NULL
            THEN 1 ELSE 0 END::float
            """,
        )
    )
    return PgCapabilities(
        server_version=server_version,
        server_version_num=server_version_num,
        has_pg_stat_statements=has_pg_stat_statements,
        has_pg_stat_io=has_pg_stat_io,
    )


def _collect_query_p95(cur: Any, capabilities: PgCapabilities, warnings: list[str]) -> float:
    if capabilities.has_pg_stat_statements:
        try:
            return _query_single_float(
                cur,
                """
                SELECT COALESCE(
                  percentile_cont(0.95) WITHIN GROUP (
                    ORDER BY (total_exec_time / NULLIF(calls, 0))
                  ),
                  0
                )::float
                FROM pg_stat_statements
                WHERE calls > 0
                """,
            )
        except Exception as exc:
            warnings.append(f"query.p95_latency_ms primary failed: {exc}")

    # Fallback for PG versions/environments without pg_stat_statements.
    warnings.append(
        "query.p95_latency_ms fallback to pg_stat_activity(active query age), "
        "result may be less stable."
    )
    try:
        return _query_single_float(
            cur,
            """
            SELECT COALESCE(
              percentile_cont(0.95) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (clock_timestamp() - query_start)) * 1000
              ),
              0
            )::float
            FROM pg_stat_activity
            WHERE state = 'active' AND query_start IS NOT NULL
            """,
        )
    except Exception as exc:
        warnings.append(f"query.p95_latency_ms fallback failed: {exc}")
        return 0.0


def _collect_io_read_latency(cur: Any, capabilities: PgCapabilities, warnings: list[str]) -> float:
    if capabilities.has_pg_stat_io:
        try:
            return _query_single_float(
                cur,
                """
                SELECT COALESCE(
                  SUM(read_time) / NULLIF(SUM(reads), 0),
                  0
                )::float
                FROM pg_stat_io
                """,
            )
        except Exception as exc:
            warnings.append(f"io.read_latency_ms primary failed: {exc}")
    warnings.append(
        "io.read_latency_ms unavailable (pg_stat_io missing or inaccessible), set to 0."
    )
    return 0.0


def collect_pg_health(dsn: str) -> PgHealthReport:
    if psycopg is None:
        return PgHealthReport(
            ok=False,
            degraded=True,
            details={"collector": "pgstat"},
            warnings=[],
            error="psycopg is not installed.",
        )

    warnings: list[str] = []
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                capabilities = _detect_capabilities(cur)
    except Exception as exc:
        return PgHealthReport(
            ok=False,
            degraded=True,
            details={"collector": "pgstat"},
            warnings=[],
            error=f"connection failed: {exc}",
        )

    metric_sources = {
        "query.p95_latency_ms": (
            "pg_stat_statements" if capabilities.has_pg_stat_statements else "pg_stat_activity_fallback"
        ),
        "wait.lock_ratio_pct": "pg_stat_activity",
        "io.read_latency_ms": "pg_stat_io" if capabilities.has_pg_stat_io else "unsupported_fallback_zero",
        "lock.blocked_sessions": "pg_locks",
        "replication.lag_sec": "pg_stat_replication",
        "buffer.hit_ratio_pct": "pg_stat_database",
    }
    degraded = False
    if not capabilities.has_pg_stat_statements:
        degraded = True
        warnings.append("pg_stat_statements extension missing; latency metric uses fallback.")
    if not capabilities.has_pg_stat_io:
        degraded = True
        warnings.append("pg_stat_io view missing; io.read_latency_ms will be 0.")

    return PgHealthReport(
        ok=True,
        degraded=degraded,
        details={
            "collector": "pgstat",
            "server_version": capabilities.server_version,
            "server_version_num": capabilities.server_version_num,
            "capabilities": {
                "has_pg_stat_statements": capabilities.has_pg_stat_statements,
                "has_pg_stat_io": capabilities.has_pg_stat_io,
            },
            "metric_sources": metric_sources,
        },
        warnings=warnings,
        error=None,
    )


def collect_pg_runtime_metrics(instance: str, dsn: str) -> PgCollectResult:
    if psycopg is None:
        raise PgCollectorError(
            "psycopg is not installed. Install psycopg and rerun with --source pgstat."
        )

    warnings: list[str] = []
    values: dict[str, float] = {}
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                capabilities = _detect_capabilities(cur)
                values["query.p95_latency_ms"] = _collect_query_p95(cur, capabilities, warnings)
                values["wait.lock_ratio_pct"] = _query_single_float(
                    cur,
                    """
                    SELECT CASE WHEN COUNT(*) = 0 THEN 0
                      ELSE 100.0 * SUM(CASE WHEN wait_event_type = 'Lock' THEN 1 ELSE 0 END) / COUNT(*)
                    END::float
                    FROM pg_stat_activity
                    WHERE state = 'active'
                    """,
                )
                values["io.read_latency_ms"] = _collect_io_read_latency(cur, capabilities, warnings)
                values["lock.blocked_sessions"] = _query_single_float(
                    cur,
                    """
                    SELECT COALESCE(COUNT(*)::float, 0)
                    FROM pg_locks
                    WHERE granted = false
                    """,
                )
                values["replication.lag_sec"] = _query_single_float(
                    cur,
                    """
                    SELECT COALESCE(MAX(EXTRACT(EPOCH FROM replay_lag)), 0)::float
                    FROM pg_stat_replication
                    """,
                )
                values["buffer.hit_ratio_pct"] = _query_single_float(
                    cur,
                    """
                    SELECT CASE WHEN SUM(blks_hit + blks_read) = 0 THEN 100
                      ELSE 100.0 * SUM(blks_hit) / SUM(blks_hit + blks_read)
                    END::float
                    FROM pg_stat_database
                    """,
                )
    except Exception as exc:
        raise PgCollectorError(f"pgstat collect failed: {exc}") from exc

    events = _build_runtime_events_from_values(instance, values)
    return PgCollectResult(events=events, warnings=warnings)
