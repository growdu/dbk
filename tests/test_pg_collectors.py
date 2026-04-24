from __future__ import annotations

from dbk.pg_collectors import _build_runtime_events_from_values


def test_build_runtime_events_from_pg_values() -> None:
    values = {
        "query.p95_latency_ms": 220.5,
        "wait.lock_ratio_pct": 35.0,
        "io.read_latency_ms": 12.3,
        "lock.blocked_sessions": 9.0,
        "replication.lag_sec": 4.2,
        "buffer.hit_ratio_pct": 91.7,
    }
    events = _build_runtime_events_from_values("pg-main-01", values)
    assert len(events) == 6
    names = {event.metric for event in events}
    assert "query.p95_latency_ms" in names
    assert "buffer.hit_ratio_pct" in names

