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


def collect_pg_runtime_metrics(instance: str, dsn: str) -> PgCollectResult:
    if psycopg is None:
        raise PgCollectorError(
            "psycopg is not installed. Install psycopg and rerun with --source pgstat."
        )

    warnings: list[str] = []

    queries = {
        "query.p95_latency_ms": """
            SELECT COALESCE(
              percentile_cont(0.95) WITHIN GROUP (
                ORDER BY (total_exec_time / NULLIF(calls, 0))
              ),
              0
            )::float
            FROM pg_stat_statements
            WHERE calls > 0
        """,
        "wait.lock_ratio_pct": """
            SELECT CASE WHEN COUNT(*) = 0 THEN 0
              ELSE 100.0 * SUM(CASE WHEN wait_event_type = 'Lock' THEN 1 ELSE 0 END) / COUNT(*)
            END::float
            FROM pg_stat_activity
            WHERE state = 'active'
        """,
        "io.read_latency_ms": """
            SELECT COALESCE(
              SUM(read_time) / NULLIF(SUM(reads), 0),
              0
            )::float
            FROM pg_stat_io
        """,
        "lock.blocked_sessions": """
            SELECT COALESCE(COUNT(*)::float, 0)
            FROM pg_locks
            WHERE granted = false
        """,
        "replication.lag_sec": """
            SELECT COALESCE(MAX(EXTRACT(EPOCH FROM replay_lag)), 0)::float
            FROM pg_stat_replication
        """,
        "buffer.hit_ratio_pct": """
            SELECT CASE WHEN SUM(blks_hit + blks_read) = 0 THEN 100
              ELSE 100.0 * SUM(blks_hit) / SUM(blks_hit + blks_read)
            END::float
            FROM pg_stat_database
        """,
    }

    values: dict[str, float] = {}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for metric, query in queries.items():
                try:
                    cur.execute(query)
                    row = cur.fetchone()
                    raw = row[0] if row else 0.0
                    values[metric] = float(raw or 0.0)
                except Exception as exc:
                    values[metric] = 0.0
                    warnings.append(f"{metric}: {exc}")

    events = _build_runtime_events_from_values(instance, values)
    return PgCollectResult(events=events, warnings=warnings)

