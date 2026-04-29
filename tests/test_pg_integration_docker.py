from __future__ import annotations

import os
import random
import shutil
import socket
import string
import subprocess
import time

import pytest

from dbk.pg_collectors import collect_pg_health, collect_pg_runtime_metrics, psycopg


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    return proc.returncode == 0


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _docker_versions() -> list[str]:
    raw = os.environ.get("DBK_PG_DOCKER_VERSIONS", "16")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _start_pg_container(version: str, port: int) -> str:
    suffix = "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    name = f"dbk-pg-{version}-{suffix}"
    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "-e",
        "POSTGRES_PASSWORD=postgres",
        "-e",
        "POSTGRES_DB=postgres",
        "-p",
        f"{port}:5432",
        f"postgres:{version}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"docker run failed: {proc.stderr.strip()}")
    return name


def _stop_pg_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("version", _docker_versions())
def test_collect_pg_runtime_metrics_with_docker(version: str) -> None:
    if os.environ.get("DBK_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set DBK_RUN_DOCKER_TESTS=1 to run docker integration tests.")
    if psycopg is None:
        pytest.skip("psycopg not installed.")
    if not _docker_available():
        pytest.skip("Docker not available.")

    port = _pick_free_port()
    container = _start_pg_container(version, port)
    try:
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
        report = None
        for _ in range(40):
            report = collect_pg_health(dsn)
            if report.ok:
                break
            time.sleep(0.5)
        assert report is not None
        assert report.ok is True

        result = collect_pg_runtime_metrics(instance=f"pg-docker-{version}", dsn=dsn)
        assert len(result.events) == 6
        metrics = {event.metric for event in result.events}
        assert "query.p95_latency_ms" in metrics
        assert "buffer.hit_ratio_pct" in metrics
    finally:
        _stop_pg_container(container)

