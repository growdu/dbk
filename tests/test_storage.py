from __future__ import annotations

from pathlib import Path

from dbk.collectors import collect_mock_runtime_metrics
from dbk.storage import RuntimeStore


def test_insert_and_query_metrics(tmp_path: Path) -> None:
    db = tmp_path / "runtime.sqlite"
    store = RuntimeStore(db)
    store.init_schema()
    events = collect_mock_runtime_metrics("pg-test-01")
    inserted = store.insert_events(events)

    assert inserted == 10
    rows = store.query_latest_metric("query.p95_latency_ms", instance="pg-test-01", limit=1)
    assert len(rows) == 1
    assert rows[0]["metric"] == "query.p95_latency_ms"

