from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import RuntimeStore
from .tracing import run_trace_profile


THRESHOLDS = {
    "query.p95_latency_ms": 200.0,
    "wait.lock_ratio_pct": 30.0,
    "io.read_latency_ms": 10.0,
    "lock.blocked_sessions": 5.0,
    "replication.lag_sec": 3.0,
    "buffer.hit_ratio_pct": 95.0,  # lower is worse
}


@dataclass(slots=True)
class DiagnosisResult:
    task_id: str
    verdict: str
    findings: list[str]
    evidence_bundle: Path
    trace_summary: Path | None


def _extract_latest_value(rows: list[Any]) -> float | None:
    if not rows:
        return None
    return float(rows[0]["value"])


def diagnose_latency_incident(
    *,
    store: RuntimeStore,
    instance: str,
    task_id: str,
    artifacts_root: Path,
    auto_trace: bool = True,
) -> DiagnosisResult:
    metrics = [
        "query.p95_latency_ms",
        "wait.lock_ratio_pct",
        "io.read_latency_ms",
        "lock.blocked_sessions",
        "replication.lag_sec",
        "buffer.hit_ratio_pct",
    ]
    latest: dict[str, float | None] = {}
    for metric in metrics:
        rows = store.query_latest_metric(metric=metric, instance=instance, limit=1)
        latest[metric] = _extract_latest_value(rows)

    findings: list[str] = []
    latency = latest["query.p95_latency_ms"]
    lock_ratio = latest["wait.lock_ratio_pct"]
    io_latency = latest["io.read_latency_ms"]
    blocked = latest["lock.blocked_sessions"]
    hit_ratio = latest["buffer.hit_ratio_pct"]

    if latency is None:
        findings.append("No runtime metrics found for target instance.")
        verdict = "insufficient_data"
    else:
        if latency > THRESHOLDS["query.p95_latency_ms"]:
            findings.append(f"p95 latency elevated: {latency:.2f}ms")
        if lock_ratio is not None and lock_ratio > THRESHOLDS["wait.lock_ratio_pct"]:
            findings.append(f"lock wait ratio elevated: {lock_ratio:.2f}%")
        if io_latency is not None and io_latency > THRESHOLDS["io.read_latency_ms"]:
            findings.append(f"io read latency elevated: {io_latency:.2f}ms")
        if blocked is not None and blocked > THRESHOLDS["lock.blocked_sessions"]:
            findings.append(f"blocked sessions high: {blocked:.0f}")
        if hit_ratio is not None and hit_ratio < THRESHOLDS["buffer.hit_ratio_pct"]:
            findings.append(f"buffer hit ratio low: {hit_ratio:.2f}%")
        verdict = "anomaly" if findings else "normal"

    trace_summary_path: Path | None = None
    if auto_trace and verdict == "anomaly":
        profile = "cpu-hotpath"
        if io_latency is not None and io_latency > THRESHOLDS["io.read_latency_ms"]:
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

    payload = {
        "task_id": task_id,
        "instance": instance,
        "verdict": verdict,
        "latest_metrics": latest,
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
        "",
        "## Findings",
    ]
    if findings:
        runbook.extend([f"- {item}" for item in findings])
    else:
        runbook.append("- no anomaly found")
    runbook.extend(
        [
            "",
            "## Next Validation Commands",
            "- `SELECT * FROM pg_stat_activity WHERE wait_event IS NOT NULL;`",
            "- `SELECT * FROM pg_locks WHERE granted = false;`",
            "- `EXPLAIN (ANALYZE, BUFFERS) <slow_sql>;`",
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
    )

