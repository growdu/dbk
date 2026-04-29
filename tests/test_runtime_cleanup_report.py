from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dbk.config import runtime_cleanup_history_path
from dbk.runtime_cleanup_daemon import build_cleanup_report, read_cleanup_history


def test_read_cleanup_history_and_report(tmp_path: Path) -> None:
    history = runtime_cleanup_history_path(tmp_path)
    history.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "ts": "2026-01-01T00:00:00+00:00",
            "ok": True,
            "summary": {
                "runtime_metrics": {"deleted": 3},
                "trace_artifacts_db": {"deleted": 1},
                "artifact_dirs": {"deleted": 2},
            },
        },
        {
            "ts": "2026-01-01T01:00:00+00:00",
            "ok": False,
            "error": "boom",
        },
    ]
    history.write_text("\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8")

    parsed = read_cleanup_history(limit=10, cwd=tmp_path)
    assert len(parsed) == 2

    report = build_cleanup_report(limit=10, cwd=tmp_path)
    assert report["total_runs"] == 2
    assert report["failed_runs"] == 1
    assert report["totals"]["runtime_metrics_deleted"] == 3
    assert report["totals"]["trace_artifacts_deleted"] == 1
    assert report["totals"]["artifact_dirs_deleted"] == 2


def test_cleanup_report_window_hours_filter(tmp_path: Path) -> None:
    history = runtime_cleanup_history_path(tmp_path)
    history.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=timezone.utc)
    lines = [
        {
            "ts": (now - timedelta(hours=48)).isoformat(),
            "ok": True,
            "summary": {
                "runtime_metrics": {"deleted": 5},
                "trace_artifacts_db": {"deleted": 2},
                "artifact_dirs": {"deleted": 1},
            },
        },
        {
            "ts": (now - timedelta(hours=2)).isoformat(),
            "ok": True,
            "summary": {
                "runtime_metrics": {"deleted": 7},
                "trace_artifacts_db": {"deleted": 3},
                "artifact_dirs": {"deleted": 4},
            },
        },
    ]
    history.write_text("\n".join(json.dumps(item) for item in lines) + "\n", encoding="utf-8")

    report = build_cleanup_report(limit=10, window_hours=24, cwd=tmp_path)
    assert report["total_runs"] == 1
    assert report["totals"]["runtime_metrics_deleted"] == 7
    assert report["totals"]["trace_artifacts_deleted"] == 3
    assert report["totals"]["artifact_dirs_deleted"] == 4
