from __future__ import annotations

import random

from .models import RuntimeEvent


def collect_mock_runtime_metrics(instance: str) -> list[RuntimeEvent]:
    # Deterministic enough for repeatable local runs while still looking realistic.
    base = random.Random(instance)
    p95_latency = max(30.0, min(400.0, base.gauss(180, 80)))
    lock_ratio = max(1.0, min(80.0, base.gauss(15, 10)))
    read_latency = max(0.2, min(30.0, base.gauss(6, 4)))
    blocked_sessions = max(0, min(40, int(base.gauss(3, 3))))
    repl_lag_sec = max(0.0, min(20.0, base.gauss(1.2, 1.0)))
    hit_ratio = max(70.0, min(99.9, base.gauss(97.0, 2.0)))

    return [
        RuntimeEvent.create(
            instance=instance,
            source="mock.sql",
            category="query",
            metric="query.p95_latency_ms",
            value=p95_latency,
            labels={"unit": "ms"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="mock.sql",
            category="wait",
            metric="wait.lock_ratio_pct",
            value=lock_ratio,
            labels={"unit": "percent"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="mock.host",
            category="io",
            metric="io.read_latency_ms",
            value=read_latency,
            labels={"unit": "ms"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="mock.sql",
            category="lock",
            metric="lock.blocked_sessions",
            value=float(blocked_sessions),
            labels={"unit": "count"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="mock.sql",
            category="replication",
            metric="replication.lag_sec",
            value=repl_lag_sec,
            labels={"unit": "seconds"},
        ),
        RuntimeEvent.create(
            instance=instance,
            source="mock.sql",
            category="buffer",
            metric="buffer.hit_ratio_pct",
            value=hit_ratio,
            labels={"unit": "percent"},
        ),
    ]

