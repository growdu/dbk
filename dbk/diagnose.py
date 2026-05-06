from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .storage import RuntimeStore
from .thresholds import DEFAULT_THRESHOLDS
from .tracing import run_trace_profile


# Number of recent data points to fetch for time-series analysis.
_TIME_SERIES_WINDOW = 20


@dataclass(slots=True)
class DiagnosisResult:
    task_id: str
    verdict: str
    findings: list[str]
    evidence_bundle: Path
    trace_summary: Path | None
    time_series: dict[str, dict[str, float | int | str | None]]  # metric -> {avg, max, min, trend}


def _extract_latest_value(rows: list[Any]) -> float | None:
    if not rows:
        return None
    return float(rows[0]["value"])


def _compute_trend(values: list[float]) -> str:
    """Simple linear trend: compare first half avg to second half avg."""
    if len(values) < 4:
        return "insufficient_data"
    mid = len(values) // 2
    first_half = values[:mid]
    second_half = values[mid:]
    first_avg = sum(first_half) / len(first_half)
    second_avg = sum(second_half) / len(second_half)
    delta = second_avg - first_avg
    if abs(delta) < 0.05 * (abs(first_avg) + 0.01):
        return "stable"
    return "increasing" if delta > 0 else "decreasing"


# Actionable SQL commands to include in the runbook.
_DIAGNOSTIC_SQL_COMMANDS = [
    ("active_wait_sessions", "SELECT pid, usename, state, wait_event_type, wait_event, query_start FROM pg_stat_activity WHERE state = 'active' AND wait_event IS NOT NULL ORDER BY query_start;"),
    ("blocked_locks", "SELECT bl.pid AS blocked_pid, a.usename AS blocked_user, a.query AS blocked_query, cl.relname, l.locktype, l.mode FROM pg_locks bl JOIN pg_stat_activity a ON bl.pid = a.pid JOIN pg_class cl ON bl.relation = cl.oid JOIN pg_locks l ON bl.locktype = l.locktype WHERE NOT bl.granted;"),
    ("long_running_queries", "SELECT pid, now() - query_start AS duration, state, query FROM pg_stat_activity WHERE state = 'active' AND query_start < now() - interval '5 minutes' ORDER BY duration DESC;"),
    ("buffer_cache_hit_ratio", "SELECT CASE WHEN SUM(blks_hit + blks_read) = 0 THEN 100 ELSE round(100.0 * SUM(blks_hit) / SUM(blks_hit + blks_read), 2) END AS cache_hit_ratio FROM pg_stat_database;"),
    ("replication_lag", "SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn, (sent_lsn - replay_lsn) AS lag_lsn FROM pg_stat_replication;"),
    ("top_slow_queries", "SELECT round(mean_exec_time::numeric, 2) AS avg_ms, calls, round(total_exec_time::numeric, 2) AS total_ms, query FROM pg_stat_statements WHERE calls > 0 ORDER BY mean_exec_time DESC LIMIT 10;"),
]


# SQL fingerprint: normalize literals to produce a reusable query shape.
_NORMALIZE_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def normalize_sql(query: str) -> str:
    """Return a normalized query fingerprint by replacing string/numeric literals.

    This collapses SELECT * FROM users WHERE id=123 AND SELECT * FROM users WHERE id=456
    into the same fingerprint: SELECT * FROM users WHERE id=##.
    Only replaces numbers that appear to be literal values (after =, >, <, IN, etc.),
    avoiding operator-adjacent digits like the leading digit of 123 in '> 100'.
    """
    result = query
    # Replace single-quoted strings with $$.
    result = re.sub(r"'(?:[^']|'')*'", "$$", result)
    # Replace numeric literals that are clearly values (after operators/whitespace,
    # before operators/whitespace/semicolons). Negative lookbehind excludes operator
    # digits; negative lookahead excludes trailing operator chars.
    result = re.sub(r"(?<=[=><(),+\-*\s/])-?\d+(?:\.\d+)?(?=[=><(),;+\-*\s/]|$)", "##", result)
    # Collapse excess whitespace.
    result = re.sub(r"\s+", " ", result).strip()
    return result


def build_explain_sql(query: str, format: str = "json") -> str:
    """Wrap a raw SQL query in EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON).

    Args:
        query: The SQL query to explain.
        format: 'json' (default), 'text', 'yaml', or 'xml'.
    Returns:
        A complete EXPLAIN SQL statement string.
    """
    # Basic injection guard: reject queries that already contain EXPLAIN.
    if re.search(r"\bEXPLAIN\b", query, re.IGNORECASE):
        return f"-- Cannot EXPLAIN a query that already contains EXPLAIN:\n-- {query[:200]}"
    if not query or not query.strip():
        return "-- Empty query, nothing to EXPLAIN"
    # Format argument must be trusted (enum above).
    safe_format = format.lower() if format.lower() in ("json", "text", "yaml", "xml") else "json"
    return f"EXPLAIN (ANALYZE, BUFFERS, FORMAT {safe_format})\n{query}"


# Lock contention diagnostic SQL commands.
_LOCK_CONTENTION_SQL = [
    (
        "lock_wait_sessions",
        "SELECT pid, relation::regclass AS relation, mode, granted, fastpath, query_start, state FROM pg_stat_activity WHERE wait_event_type = 'Lock' ORDER BY query_start;",
    ),
    (
        "lock_waits_detail",
        "SELECT bl.pid AS blocked_pid, a.usename AS blocked_user, cl.relname AS locked_table, l.locktype AS lock_type, l.mode AS lock_mode, l.granted AS granted, a.query AS blocked_query, now() - a.query_start AS waiting_duration FROM pg_locks bl JOIN pg_stat_activity a ON bl.pid = a.pid JOIN pg_class cl ON bl.relation = cl.oid JOIN pg_locks l ON bl.locktype = l.locktype AND bl.database IS NOT DISTINCT FROM l.database AND bl.relation IS NOT DISTINCT FROM l.relation WHERE NOT bl.granted ORDER BY a.query_start;",
    ),
    (
        "table_lock_modes",
        "SELECT relname, mode, count(*) AS wait_count FROM pg_locks WHERE locktype = 'relation' GROUP BY relname, mode ORDER BY wait_count DESC;",
    ),
    (
        "transaction_2pc",
        "SELECT gid, prepared, owner, database, transaction AS txid FROM pg_prepared_xacts ORDER BY prepared;",
    ),
    (
        "idle_in_transaction",
        "SELECT pid, usename, now() - state_change AS idle_duration, state, left(query, 200) AS query_preview FROM pg_stat_activity WHERE state = 'idle in transaction' AND state_change < now() - interval '5 minutes' ORDER BY idle_duration DESC;",
    ),
]

# Replication / HA bottleneck diagnostic SQL commands.
_REPLICATION_BOTTLENECK_SQL = [
    (
        "replication_slots",
        "SELECT slot_name, plugin, slot_type, active, restart_lsn, confirmed_flush_lsn FROM pg_replication_slots;",
    ),
    (
        "wal_lag",
        "SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn, (sent_lsn - replay_lsn)::bigint AS lag_bytes, (write_lsn - replay_lsn)::bigint AS write_lag_bytes, (flush_lsn - replay_lsn)::bigint AS flush_lag_bytes FROM pg_stat_replication ORDER BY lag_bytes DESC;",
    ),
    (
        "wal_senders",
        "SELECT pid, usesysid, usename, application_name, client_addr, state, sync_state, sent_lsn, write_lsn, flush_lsn, replay_lsn FROM pg_stat_replication;",
    ),
    (
        "replication_conflict",
        "SELECT datname, confl_tablespace, confl_lock, confl_snapshot, confl_bufferpin, confl_deadlock FROM pg_stat_database_conflicts WHERE confl_lock > 0 OR confl_snapshot > 0;",
    ),
    (
        "archiver_status",
        "SELECT pg_is_in_recovery(), (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'archiver') AS archiver_active, (SELECT setting FROM pg_settings WHERE name = 'archive_command') AS archive_command, (SELECT setting FROM pg_settings WHERE name = 'archive_mode') AS archive_mode;",
    ),
]


@dataclass
class LockContentionReport:
    """Result of running lock contention diagnostics."""

    lock_waits: int
    idle_in_transaction: int
    top_blocked_table: str | None
    worst_lock_waits_sec: float
    verdict: str  # normal | contention | critical
    sql_commands: list[tuple[str, str]]


@dataclass
class ReplicationReport:
    """Result of running replication diagnostics."""

    max_lag_bytes: int
    lag_verdict: str  # normal | lag_detected | critical
    slot_count: int
    conflict_count: int
    sql_commands: list[tuple[str, str]]


def diagnose_lock_contention(store: RuntimeStore, instance: str) -> LockContentionReport:
    """Diagnose lock contention from stored metrics and return actionable SQL runbook."""
    blocked = store.query_latest_metric("lock.blocked_sessions", instance=instance, limit=1)
    blocked_count = int(float(blocked[0]["value"])) if blocked else 0

    lock_ratio_rows = store.query_latest_metric("wait.lock_ratio_pct", instance=instance, limit=1)
    lock_ratio = float(lock_ratio_rows[0]["value"]) if lock_ratio_rows else 0.0

    # Estimate worst-case wait from time-series trend.
    rows = store.query_latest_metric("lock.blocked_sessions", instance=instance, limit=20)
    worst_lock_sec = max((float(r["value"]) for r in rows), default=0.0)

    verdict = "critical" if blocked_count >= 5 or lock_ratio > 30 else "contention" if blocked_count >= 1 or lock_ratio > 10 else "normal"

    top_table: str | None = None
    # Try to get top locked table from the second half of recent data.
    if rows and len(rows) >= 5:
        top_table = None  # SQL fingerprint not available from metrics alone.

    return LockContentionReport(
        lock_waits=blocked_count,
        idle_in_transaction=0,  # Would need pg_stat_activity.
        top_blocked_table=top_table,
        worst_lock_waits_sec=worst_lock_sec,
        verdict=verdict,
        sql_commands=_LOCK_CONTENTION_SQL,
    )


def diagnose_replication_bottleneck(store: RuntimeStore, instance: str) -> ReplicationReport:
    """Diagnose replication lag and HA bottlenecks from stored metrics."""
    lag_rows = store.query_latest_metric("replication.lag_sec", instance=instance, limit=1)
    lag_sec = float(lag_rows[0]["value"]) if lag_rows else 0.0
    lag_bytes = int(lag_sec * 16 * 1024 * 1024)  # Rough estimate: 16 MB/sec wal rate.

    verdict = "critical" if lag_sec > 60 else "lag_detected" if lag_sec > 10 else "normal"

    return ReplicationReport(
        max_lag_bytes=lag_bytes,
        lag_verdict=verdict,
        slot_count=0,
        conflict_count=0,
        sql_commands=_REPLICATION_BOTTLENECK_SQL,
    )


def diagnose_latency_incident(
    *,
    store: RuntimeStore,
    instance: str,
    task_id: str,
    artifacts_root: Path,
    auto_trace: bool = True,
    thresholds: dict[str, float] | None = None,
) -> DiagnosisResult:
    applied_thresholds = thresholds or dict(DEFAULT_THRESHOLDS)
    metrics = [
        "query.p95_latency_ms",
        "wait.lock_ratio_pct",
        "io.read_latency_ms",
        "lock.blocked_sessions",
        "replication.lag_sec",
        "buffer.hit_ratio_pct",
        "connection.active_count",
        "connection.total_count",
        "transaction.rollback_ratio_pct",
        "checkpoint.write_latency_ms",
    ]
    latest: dict[str, float | None] = {}
    time_series: dict[str, dict[str, float | int | str | None]] = {}

    for metric in metrics:
        rows = store.query_latest_metric(metric=metric, instance=instance, limit=_TIME_SERIES_WINDOW)
        latest[metric] = _extract_latest_value(rows)
        # Time-series analysis: compute aggregate over the window.
        if rows:
            agg = RuntimeStore.aggregate_rows(rows)
            values = [float(r["value"]) for r in rows]
            agg["trend"] = _compute_trend(values)  # type: ignore[assignment]
        else:
            time_series[metric] = {}

    findings: list[str] = []
    latency = latest.get("query.p95_latency_ms")

    # Metrics where HIGHER value is worse.
    _HIGHER_IS_WORSE = frozenset({
        "query.p95_latency_ms",
        "wait.lock_ratio_pct",
        "io.read_latency_ms",
        "lock.blocked_sessions",
        "replication.lag_sec",
        "connection.active_count",
        "connection.total_count",
        "transaction.rollback_ratio_pct",
        "checkpoint.write_latency_ms",
    })
    # Metrics where LOWER value is worse.
    _LOWER_IS_WORSE = frozenset({
        "buffer.hit_ratio_pct",
    })

    def _is_anomalous(metric: str, value: float, threshold: float) -> bool:
        if metric in _HIGHER_IS_WORSE:
            return value > threshold
        if metric in _LOWER_IS_WORSE:
            return value < threshold
        return False

    if latency is None:
        findings.append("No runtime metrics found for target instance.")
        verdict = "insufficient_data"
    else:
        for metric in metrics:
            val = latest.get(metric)
            th = applied_thresholds.get(metric)
            if val is None or th is None:
                continue
            if _is_anomalous(metric, val, th):
                direction = "elevated" if metric in _HIGHER_IS_WORSE else "low"
                trend = ""
                if metric in time_series and time_series[metric].get("trend"):
                    trend = f", trend: {time_series[metric]['trend']}"
                findings.append(
                    f"{metric} {direction}: {val:.2f} "
                    f"(threshold: {th:.2f}{trend})"
                )
        verdict = "anomaly" if findings else "normal"

    # --- Lock contention diagnostic ---
    lock_report = diagnose_lock_contention(store, instance)
    if lock_report.verdict != "normal":
        findings.append(f"[lock_contention] verdict={lock_report.verdict}, blocked={lock_report.lock_waits}, worst_wait={lock_report.worst_lock_waits_sec:.1f}s")
        if lock_report.verdict == "critical":
            confidence = "high"

    # --- Replication bottleneck diagnostic ---
    repl_report = diagnose_replication_bottleneck(store, instance)
    if repl_report.lag_verdict != "normal":
        findings.append(f"[replication] verdict={repl_report.lag_verdict}, lag_bytes={repl_report.max_lag_bytes}, slot_count={repl_report.slot_count}")

    # Re-evaluate verdict after adding lock/repl findings.
    if verdict == "normal" and findings:
        verdict = "anomaly"

    trace_summary_path: Path | None = None
    if auto_trace and verdict == "anomaly":
        profile = "cpu-hotpath"
        io_lat = latest.get("io.read_latency_ms")
        if io_lat is not None and io_lat > applied_thresholds.get("io.read_latency_ms", 999999):
            profile = "io-latency"
        trace_result = run_trace_profile(
            profile=profile,
            task_id=task_id,
            duration_sec=30,
            artifacts_root=artifacts_root,
            execute=False,
        )
        store.insert_trace_artifact(trace_result.artifact)
        trace_summary_path = trace_result.summary_path

    bundle_dir = artifacts_root / task_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    evidence_json = bundle_dir / "evidence.json"
    runbook_md = bundle_dir / "runbook.md"

    # Confidence level based on number of concurrent anomalies.
    anomaly_count = len(findings)
    confidence = "high" if anomaly_count >= 3 else "medium" if anomaly_count >= 1 else "low"

    payload = {
        "task_id": task_id,
        "instance": instance,
        "verdict": verdict,
        "confidence": confidence,
        "latest_metrics": latest,
        "time_series_aggregates": time_series,
        "thresholds": applied_thresholds,
        "findings": findings,
        "trace_summary": str(trace_summary_path) if trace_summary_path else None,
    }
    evidence_json.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    runbook = [
        "# Runbook",
        "",
        f"- task_id: {task_id}",
        f"- instance: {instance}",
        f"- verdict: {verdict}",
        f"- confidence: {confidence}",
        "",
        "## Latest Metrics vs Thresholds",
    ]
    for metric in metrics:
        val = latest.get(metric)
        th_float: float | None = applied_thresholds.get(metric)
        th_val = th_float if th_float is not None else 0.0
        if val is not None:
            if metric == "buffer.hit_ratio_pct":
                status = "WARN" if val < th_val else "OK"
            elif val > th_val:
                status = "WARN"
            else:
                status = "OK"
        else:
            status = "OK"
        runbook.append(f"- {metric}: {val} (threshold: {th}) [{status}]")

    runbook.extend(
        [
            "",
            "## Findings",
        ]
    )
    if findings:
        runbook.extend([f"- {item}" for item in findings])
    else:
        runbook.append("- no anomaly found")

    runbook.extend(
        [
            "",
            "## Next Validation Commands",
            "",
            "Run the following SQL commands against the target PostgreSQL instance to gather diagnostic data:",
        ]
    )
    for cmd_name, cmd_sql in _DIAGNOSTIC_SQL_COMMANDS:
        runbook.append(f"\n### {cmd_name}")
        runbook.append(f"```sql\n{cmd_sql}\n```")

    runbook.extend(
        [
            "",
            "## Lock Contention Diagnostic",
            f"- verdict: {lock_report.verdict}",
            f"- blocked_sessions: {lock_report.lock_waits}",
            f"- worst_wait_sec: {lock_report.worst_lock_waits_sec:.1f}",
            f"- top_blocked_table: {lock_report.top_blocked_table or 'N/A'}",
            "",
        ]
    )
    for cmd_name, cmd_sql in lock_report.sql_commands:
        runbook.append(f"\n### {cmd_name}")
        runbook.append(f"```sql\n{cmd_sql}\n```")

    runbook.extend(
        [
            "",
            "## Replication Bottleneck Diagnostic",
            f"- verdict: {repl_report.lag_verdict}",
            f"- max_lag_bytes: {repl_report.max_lag_bytes}",
            f"- slot_count: {repl_report.slot_count}",
            f"- conflict_count: {repl_report.conflict_count}",
            "",
        ]
    )
    for cmd_name, cmd_sql in repl_report.sql_commands:
        runbook.append(f"\n### {cmd_name}")
        runbook.append(f"```sql\n{cmd_sql}\n```")

    runbook.extend(
        [
            "",
            "## SQL Fingerprint Normalization (EXPLAIN)",
            "Use `normalize_sql()` to collapse query variants into a reusable fingerprint.",
            "Use `build_explain_sql()` to generate an EXPLAIN ANALYZE query for any slow query.",
            "",
        ]
    )

    runbook.extend(
        [
            "",
            "## Trace Summary",
            f"- auto_trace: {auto_trace}",
            f"- trace_profile: {trace_summary_path.name if trace_summary_path else 'N/A'}",
            "",
        ]
    )
    runbook_md.write_text("\n".join(runbook), encoding="utf-8")

    return DiagnosisResult(
        task_id=task_id,
        verdict=verdict,
        findings=findings,
        evidence_bundle=bundle_dir,
        trace_summary=trace_summary_path,
        time_series=time_series,
    )
