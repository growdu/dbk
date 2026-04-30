from __future__ import annotations

import json
from dataclasses import dataclass
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
            agg["trend"] = _compute_trend(values)
            time_series[metric] = agg
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
        th = applied_thresholds.get(metric, "N/A")
        status = "OK" if val is None else ("WARN" if (isinstance(th, float) and metric != "buffer.hit_ratio_pct" and val > th) or (metric == "buffer.hit_ratio_pct" and isinstance(val, float) and val < th) else "OK")
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
