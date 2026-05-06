from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import RuntimeEvent

try:
    import psycopg
except Exception:  # pragma: no cover - tested by behavior, not import internals
    psycopg = None


class PgCollectorError(RuntimeError):
    """Raised when PostgreSQL collector cannot run."""


@dataclass(slots=True)
class PgCollectResult:
    events: list[RuntimeEvent]
    warnings: list[str]


# PostgreSQL version compatibility map.
# Maps server_version_num (e.g. 160004) to a set of supported features.
_PG_VERSION_FEATURES: dict[int, set[str]] = {
    # PG 14: introduced pg_stat_io (partial), still missing some counters.
    140005: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    140006: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    140007: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    140008: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    140009: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    # PG 15: same as 14 for our metrics.
    150001: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    150002: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    150003: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    150004: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    150005: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    150006: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    150007: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    150008: {"pg_stat_statements", "pg_stat_io_partial", "pg_stat_bgwriter", "pg_stat_activity"},
    # PG 16: full pg_stat_io support.
    160001: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    160002: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    160003: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    160004: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    160005: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    160006: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    # PG 17: same as 16 with additional features.
    170000: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    170001: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    170002: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    170003: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    170004: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    170005: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    170006: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
    170007: {"pg_stat_statements", "pg_stat_io", "pg_stat_bgwriter", "pg_stat_activity"},
}


def _pg_features_for_version(version_num: int) -> set[str]:
    """Return the feature set for a given PG version number.

    Falls back to the closest lower major version if exact version not found.
    """
    if version_num in _PG_VERSION_FEATURES:
        return _PG_VERSION_FEATURES[version_num]
    # Fallback: find the closest lower major.minor entry.
    candidates = sorted([v for v in _PG_VERSION_FEATURES if v <= version_num], reverse=True)
    if candidates:
        return _PG_VERSION_FEATURES[candidates[0]]
    # Unknown version: assume minimal feature set.
    return {"pg_stat_statements", "pg_stat_bgwriter", "pg_stat_activity"}


@dataclass(slots=True)
class PgCapabilities:
    server_version: str
    server_version_num: int
    has_pg_stat_statements: bool
    has_pg_stat_io: bool
    has_pg_stat_bgwriter: bool
    supported_features: set[str]


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
    events = [
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
        # New metrics:
        RuntimeEvent.create(
            instance=instance,
            source="pg_stat_activity",
            category="connection",
            metric="connection.active_count",
            value=values["connection.active_count"],
            labels={"unit": "count", "source": "pg"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="pg_stat_activity",
            category="connection",
            metric="connection.total_count",
            value=values["connection.total_count"],
            labels={"unit": "count", "source": "pg"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="pg_stat_database",
            category="transaction",
            metric="transaction.rollback_ratio_pct",
            value=values["transaction.rollback_ratio_pct"],
            labels={"unit": "percent", "source": "pg"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="pg_stat_bgwriter",
            category="checkpoint",
            metric="checkpoint.write_latency_ms",
            value=values["checkpoint.write_latency_ms"],
            labels={"unit": "ms", "source": "pg"},
        ),
    ]
    return events


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
    has_pg_stat_bgwriter = bool(
        _query_single_float(
            cur,
            """
            SELECT CASE WHEN to_regclass('pg_catalog.pg_stat_bgwriter') IS NOT NULL
            THEN 1 ELSE 0 END::float
            """,
        )
    )
    supported_features = _pg_features_for_version(server_version_num)
    return PgCapabilities(
        server_version=server_version,
        server_version_num=server_version_num,
        has_pg_stat_statements=has_pg_stat_statements,
        has_pg_stat_io=has_pg_stat_io,
        has_pg_stat_bgwriter=has_pg_stat_bgwriter,
        supported_features=supported_features,
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
        "connection.active_count": "pg_stat_activity",
        "connection.total_count": "pg_stat_activity",
        "transaction.rollback_ratio_pct": "pg_stat_database",
        "checkpoint.write_latency_ms": "pg_stat_bgwriter" if capabilities.has_pg_stat_bgwriter else "unsupported_fallback_zero",
    }
    degraded = False
    if not capabilities.has_pg_stat_statements:
        degraded = True
        warnings.append("pg_stat_statements extension missing; latency metric uses fallback.")
    if not capabilities.has_pg_stat_io:
        degraded = True
        warnings.append("pg_stat_io view missing; io.read_latency_ms will be 0.")
    if not capabilities.has_pg_stat_bgwriter:
        degraded = True
        warnings.append("pg_stat_bgwriter view missing; checkpoint.write_latency_ms will be 0.")

    return PgHealthReport(
        ok=True,
        degraded=degraded,
        details={
            "collector": "pgstat",
            "server_version": capabilities.server_version,
            "server_version_num": capabilities.server_version_num,
            "supported_features": sorted(capabilities.supported_features),
            "capabilities": {
                "has_pg_stat_statements": capabilities.has_pg_stat_statements,
                "has_pg_stat_io": capabilities.has_pg_stat_io,
                "has_pg_stat_bgwriter": capabilities.has_pg_stat_bgwriter,
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
                # New metrics:
                values["connection.active_count"] = _query_single_float(
                    cur,
                    """
                    SELECT COUNT(*)::float
                    FROM pg_stat_activity
                    WHERE state = 'active'
                    """,
                )
                values["connection.total_count"] = _query_single_float(
                    cur,
                    """
                    SELECT COUNT(*)::float
                    FROM pg_stat_activity
                    """,
                )
                values["transaction.rollback_ratio_pct"] = _query_single_float(
                    cur,
                    """
                    SELECT CASE WHEN SUM(xact_commit + xact_rollback) = 0 THEN 0
                      ELSE 100.0 * SUM(xact_rollback) / SUM(xact_commit + xact_rollback)
                    END::float
                    FROM pg_stat_database
                    """,
                )
                # checkpoint_write_time is in ms (PG 14+).
                if capabilities.has_pg_stat_bgwriter:
                    values["checkpoint.write_latency_ms"] = _query_single_float(
                        cur,
                        """
                        SELECT COALESCE(
                          SUM(checkpoint_write_time) / NULLIF(SUM(checkpoint_write_time), 0) /
                          NULLIF(COUNT(*), 0),
                          0
                        )::float
                        FROM pg_stat_bgwriter
                        """,
                    )
                else:
                    values["checkpoint.write_latency_ms"] = 0.0
                    warnings.append("pg_stat_bgwriter unavailable; checkpoint.write_latency_ms set to 0.")
    except Exception as exc:
        raise PgCollectorError(f"pgstat collect failed: {exc}") from exc

    events = _build_runtime_events_from_values(instance, values)
    return PgCollectResult(events=events, warnings=warnings)
