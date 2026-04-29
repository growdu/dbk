from __future__ import annotations

from dbk.pg_collectors import (
    PgCapabilities,
    PgHealthReport,
    _build_runtime_events_from_values,
    _collect_io_read_latency,
    _collect_query_p95,
)


class _FakeCursor:
    def __init__(self, values: list[float]) -> None:
        self._values = values
        self._idx = 0

    def execute(self, _query: str) -> None:
        return None

    def fetchone(self) -> tuple[float]:
        value = self._values[self._idx]
        self._idx += 1
        return (value,)


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


def test_collect_query_p95_fallback_when_pg_stat_statements_missing() -> None:
    warnings: list[str] = []
    cursor = _FakeCursor([123.0])
    caps = PgCapabilities(
        server_version="15.8",
        server_version_num=150008,
        has_pg_stat_statements=False,
        has_pg_stat_io=False,
    )
    value = _collect_query_p95(cursor, caps, warnings)
    assert value == 123.0
    assert any("fallback" in item for item in warnings)


def test_collect_io_latency_fallback_zero_when_pg_stat_io_missing() -> None:
    warnings: list[str] = []
    cursor = _FakeCursor([999.0])
    caps = PgCapabilities(
        server_version="15.8",
        server_version_num=150008,
        has_pg_stat_statements=True,
        has_pg_stat_io=False,
    )
    value = _collect_io_read_latency(cursor, caps, warnings)
    assert value == 0.0
    assert any("pg_stat_io" in item for item in warnings)


def test_health_report_to_dict() -> None:
    report = PgHealthReport(
        ok=True,
        degraded=True,
        details={"collector": "pgstat"},
        warnings=["w1"],
        error=None,
    )
    payload = report.to_dict()
    assert payload["ok"] is True
    assert payload["degraded"] is True
