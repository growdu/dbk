from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dbk.models import RuntimeEvent
from dbk.storage import RuntimeStore


ROOT = Path(__file__).resolve().parents[1]


def _iso(hours_ago: float) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{env.get('PYTHONPATH', '')}".rstrip(":")
    return subprocess.run(
        [sys.executable, "-m", "dbk.cli", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_runtime_cleanup_dry_run_and_apply(tmp_path: Path) -> None:
    db = tmp_path / ".dbk" / "runtime.sqlite"
    store = RuntimeStore(db)
    store.init_schema()
    store.insert_events(
        [
            RuntimeEvent(
                ts=_iso(300),
                instance="pg-old",
                source="test",
                category="query",
                metric="query.p95_latency_ms",
                value=100.0,
                labels={},
            ),
            RuntimeEvent(
                ts=_iso(5),
                instance="pg-old",
                source="test",
                category="query",
                metric="query.p95_latency_ms",
                value=105.0,
                labels={},
            ),
        ]
    )

    dry = _run(tmp_path, "runtime", "cleanup", "--older-than-hours", "168", "--instance", "pg-old", "--dry-run")
    assert dry.returncode == 0, dry.stderr
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["runtime_metrics"]["candidate"] == 1
    assert dry_payload["runtime_metrics"]["deleted"] == 0

    apply = _run(tmp_path, "runtime", "cleanup", "--older-than-hours", "168", "--instance", "pg-old")
    assert apply.returncode == 0, apply.stderr
    apply_payload = json.loads(apply.stdout)
    assert apply_payload["runtime_metrics"]["deleted"] == 1

