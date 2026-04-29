from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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


def test_runtime_cleanup_daemon_lifecycle(tmp_path: Path) -> None:
    start = _run(
        tmp_path,
        "runtime",
        "cleanup-daemon",
        "start",
        "--interval-sec",
        "1",
        "--older-than-hours",
        "1",
    )
    assert start.returncode == 0, start.stderr
    start_payload = json.loads(start.stdout)
    assert start_payload["started"] is True

    time.sleep(1.2)

    status = _run(tmp_path, "runtime", "cleanup-daemon", "status")
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["running"] is True
    assert status_payload["total_runs"] >= 1

    report = _run(tmp_path, "runtime", "cleanup-report", "--limit", "20")
    assert report.returncode == 0, report.stderr
    report_payload = json.loads(report.stdout)
    assert report_payload["total_runs"] >= 1
    assert report_payload["daemon"]["running"] is True

    stop = _run(tmp_path, "runtime", "cleanup-daemon", "stop")
    assert stop.returncode == 0, stop.stderr
    stop_payload = json.loads(stop.stdout)
    assert stop_payload["stopped"] is True

    status2 = _run(tmp_path, "runtime", "cleanup-daemon", "status")
    assert status2.returncode == 2
    status2_payload = json.loads(status2.stdout)
    assert status2_payload["running"] is False
