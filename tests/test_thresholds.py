from __future__ import annotations

import json
from pathlib import Path

from dbk.thresholds import DEFAULT_THRESHOLDS, load_thresholds


def test_load_thresholds_default() -> None:
    loaded = load_thresholds()
    assert loaded == DEFAULT_THRESHOLDS


def test_load_thresholds_override(tmp_path: Path) -> None:
    cfg = tmp_path / "thresholds.json"
    cfg.write_text(
        json.dumps(
            {
                "query.p95_latency_ms": 150.0,
                "buffer.hit_ratio_pct": 96.0,
                "unknown.metric": 1,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_thresholds(cfg)
    assert loaded["query.p95_latency_ms"] == 150.0
    assert loaded["buffer.hit_ratio_pct"] == 96.0
    assert "unknown.metric" not in loaded

