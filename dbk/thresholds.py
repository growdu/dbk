from __future__ import annotations

import json
from pathlib import Path


DEFAULT_THRESHOLDS: dict[str, float] = {
    "query.p95_latency_ms": 200.0,
    "wait.lock_ratio_pct": 30.0,
    "io.read_latency_ms": 10.0,
    "lock.blocked_sessions": 5.0,
    "replication.lag_sec": 3.0,
    "buffer.hit_ratio_pct": 95.0,  # lower is worse
}


def load_thresholds(path: Path | None = None) -> dict[str, float]:
    merged = dict(DEFAULT_THRESHOLDS)
    if path is None:
        return merged
    if not path.exists():
        raise FileNotFoundError(f"Threshold file not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Threshold file must be a JSON object.")

    for key, value in payload.items():
        if key not in DEFAULT_THRESHOLDS:
            continue
        merged[key] = float(value)
    return merged

