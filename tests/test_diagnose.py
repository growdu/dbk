from __future__ import annotations

from pathlib import Path

from dbk.collectors import collect_mock_runtime_metrics
from dbk.diagnose import diagnose_latency_incident
from dbk.models import RuntimeEvent
from dbk.storage import RuntimeStore


def test_diagnose_generates_evidence_bundle(tmp_path: Path) -> None:
    db = tmp_path / "runtime.sqlite"
    store = RuntimeStore(db)
    store.init_schema()
    events = collect_mock_runtime_metrics("pg-main-01")
    events.append(
        RuntimeEvent.create(
            instance="pg-main-01",
            source="test",
            category="query",
            metric="query.p95_latency_ms",
            value=300.0,
            labels={"unit": "ms"},
        )
    )
    store.insert_events(events)
    result = diagnose_latency_incident(
        store=store,
        instance="pg-main-01",
        task_id="incident-1",
        artifacts_root=tmp_path / "artifacts",
        auto_trace=True,
    )
    assert result.verdict == "anomaly"
    assert result.evidence_bundle.exists()
    assert (result.evidence_bundle / "evidence.json").exists()
    assert result.trace_summary is not None

