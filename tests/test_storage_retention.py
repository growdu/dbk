from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from dbk.models import RuntimeEvent, TraceArtifact
from dbk.storage import RuntimeStore


def _iso(hours_ago: float) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_runtime_metric_retention_count_and_delete(tmp_path: Path) -> None:
    db = tmp_path / "runtime.sqlite"
    store = RuntimeStore(db)
    store.init_schema()
    store.insert_events(
        [
            RuntimeEvent(
                ts=_iso(200),
                instance="pg-a",
                source="test",
                category="query",
                metric="query.p95_latency_ms",
                value=100.0,
                labels={},
            ),
            RuntimeEvent(
                ts=_iso(10),
                instance="pg-a",
                source="test",
                category="query",
                metric="query.p95_latency_ms",
                value=110.0,
                labels={},
            ),
            RuntimeEvent(
                ts=_iso(200),
                instance="pg-b",
                source="test",
                category="query",
                metric="query.p95_latency_ms",
                value=120.0,
                labels={},
            ),
        ]
    )

    assert store.count_metrics_older_than(older_than_hours=168, instance="pg-a") == 1
    deleted = store.delete_metrics_older_than(older_than_hours=168, instance="pg-a")
    assert deleted == 1
    assert store.count_metrics_older_than(older_than_hours=168, instance="pg-a") == 0
    assert store.count_metrics_older_than(older_than_hours=168, instance="pg-b") == 1


def test_trace_retention_count_and_delete(tmp_path: Path) -> None:
    db = tmp_path / "runtime.sqlite"
    store = RuntimeStore(db)
    store.init_schema()
    store.insert_trace_artifact(
        TraceArtifact(
            task_id="old",
            profile="cpu-hotpath",
            started_at=_iso(300),
            duration_sec=30,
            artifact_path="/tmp/old",
            summary_json={},
        )
    )
    store.insert_trace_artifact(
        TraceArtifact(
            task_id="new",
            profile="cpu-hotpath",
            started_at=_iso(5),
            duration_sec=30,
            artifact_path="/tmp/new",
            summary_json={},
        )
    )
    assert store.count_trace_artifacts_older_than(older_than_hours=168) == 1
    assert store.delete_trace_artifacts_older_than(older_than_hours=168) == 1
    assert store.count_trace_artifacts_older_than(older_than_hours=168) == 0

