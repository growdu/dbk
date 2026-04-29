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


def test_collect_daemon_lifecycle(tmp_path: Path) -> None:
    start = _run(tmp_path, "collect", "daemon", "start", "--source", "mock", "--interval-sec", "1")
    assert start.returncode == 0, start.stderr
    started_payload = json.loads(start.stdout)
    assert started_payload["started"] is True

    # Allow daemon loop to run at least once.
    time.sleep(1.5)

    status = _run(tmp_path, "collect", "daemon", "status")
    assert status.returncode == 0, status.stderr
    status_payload = json.loads(status.stdout)
    assert status_payload["running"] is True
    assert len(status_payload["daemons"]) == 1
    assert status_payload["daemons"][0]["total_collections"] >= 1

    stop = _run(tmp_path, "collect", "daemon", "stop")
    assert stop.returncode == 0, stop.stderr
    stop_payload = json.loads(stop.stdout)
    assert stop_payload["stopped"] is True
    assert len(stop_payload["results"]) == 1

    status2 = _run(tmp_path, "collect", "daemon", "status")
    assert status2.returncode == 2
    status2_payload = json.loads(status2.stdout)
    assert status2_payload["running"] is False


def test_collect_daemon_multi_instance_list_and_stop_all(tmp_path: Path) -> None:
    start1 = _run(
        tmp_path,
        "collect",
        "daemon",
        "start",
        "--source",
        "mock",
        "--instance",
        "pg-a",
        "--interval-sec",
        "1",
        "--tags",
        "prod,read",
    )
    assert start1.returncode == 0, start1.stderr
    start2 = _run(
        tmp_path,
        "collect",
        "daemon",
        "start",
        "--source",
        "mock",
        "--instance",
        "pg-b",
        "--interval-sec",
        "1",
        "--tags",
        "dev",
    )
    assert start2.returncode == 0, start2.stderr

    time.sleep(1.0)

    listed = _run(tmp_path, "collect", "daemon", "list")
    assert listed.returncode == 0, listed.stderr
    listed_payload = json.loads(listed.stdout)
    running_instances = sorted(item["instance"] for item in listed_payload["daemons"] if item["running"])
    assert running_instances == ["pg-a", "pg-b"]

    prod_only = _run(tmp_path, "collect", "daemon", "list", "--tag", "prod")
    assert prod_only.returncode == 0, prod_only.stderr
    prod_payload = json.loads(prod_only.stdout)
    assert [item["instance"] for item in prod_payload["daemons"] if item["running"]] == ["pg-a"]

    by_pattern = _run(tmp_path, "collect", "daemon", "list", "--instance-pattern", "pg-b*")
    assert by_pattern.returncode == 0, by_pattern.stderr
    by_pattern_payload = json.loads(by_pattern.stdout)
    assert [item["instance"] for item in by_pattern_payload["daemons"] if item["running"]] == ["pg-b"]

    by_source = _run(tmp_path, "collect", "daemon", "list", "--source", "mock")
    assert by_source.returncode == 0, by_source.stderr
    by_source_payload = json.loads(by_source.stdout)
    running_mock = sorted(item["instance"] for item in by_source_payload["daemons"] if item["running"])
    assert running_mock == ["pg-a", "pg-b"]

    stopped = _run(tmp_path, "collect", "daemon", "stop", "--all")
    assert stopped.returncode == 0, stopped.stderr
    stopped_payload = json.loads(stopped.stdout)
    assert stopped_payload["stopped"] is True
    assert len(stopped_payload["results"]) == 2


def test_collect_daemon_max_running_with_preemption(tmp_path: Path) -> None:
    low = _run(
        tmp_path,
        "collect",
        "daemon",
        "start",
        "--source",
        "mock",
        "--instance",
        "pg-low",
        "--interval-sec",
        "1",
        "--priority",
        "10",
        "--max-running",
        "1",
    )
    assert low.returncode == 0, low.stderr

    blocked = _run(
        tmp_path,
        "collect",
        "daemon",
        "start",
        "--source",
        "mock",
        "--instance",
        "pg-high",
        "--interval-sec",
        "1",
        "--priority",
        "90",
        "--max-running",
        "1",
    )
    assert blocked.returncode == 2
    assert "max_running limit reached" in blocked.stderr

    preempted = _run(
        tmp_path,
        "collect",
        "daemon",
        "start",
        "--source",
        "mock",
        "--instance",
        "pg-high",
        "--interval-sec",
        "1",
        "--priority",
        "90",
        "--max-running",
        "1",
        "--preempt-lower-priority",
    )
    assert preempted.returncode == 0, preempted.stderr

    listed = _run(tmp_path, "collect", "daemon", "list")
    listed_payload = json.loads(listed.stdout)
    running_instances = [item["instance"] for item in listed_payload["daemons"] if item["running"]]
    assert running_instances == ["pg-high"]

    _run(tmp_path, "collect", "daemon", "stop", "--all")
