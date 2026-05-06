"""PostgreSQL multi-version integration test matrix.

Covers PG 14 / 15 / 16 / 17 with:
  - Health checks
  - Runtime metrics collection
  - Version-specific feature detection
  - Lock contention diagnostics
  - Replication bottleneck diagnostics
  - Cleanup operations

Run with::

    # Start containers
    docker-compose -f integration/docker-compose.yml up -d

    # Run tests
    DBK_RUN_DOCKER_TESTS=1 python -m pytest tests/test_pg_integration_matrix.py -v

    # Tear down
    docker-compose -f integration/docker-compose.yml down

Or use the runner script::

    python scripts/run_pg_integration_matrix.py
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from dbk.diagnose import (
    diagnose_lock_contention,
    diagnose_replication_bottleneck,
)
from dbk.pg_collectors import (
    PgCapabilities,
    _pg_features_for_version,
    collect_pg_health,
    collect_pg_runtime_metrics,
    psycopg,
)
from dbk.sdk import DBKClient, DBKConfigError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PG_PORTS = {
    "14": 5433,
    "15": 5434,
    "16": 5435,
    "17": 5436,
}
PG_PASSWORD = "dbk_secret"


def _pg_dsn(port: int) -> str:
    return f"postgresql://postgres:{PG_PASSWORD}@127.0.0.1:{port}/postgres"


def _wait_for_pg(port: int, timeout: int = 30) -> bool:
    """Wait for PostgreSQL to accept connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = psycopg.connect(
                conninfo=_pg_dsn(port), connect_timeout=2, autocommit=True
            )
            conn.close()
            return True
        except Exception:
            time.sleep(1)
    return False


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, check=False
    )
    return r.returncode == 0


def _skip_if_no_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker not available")


def _skip_if_no_psycopg() -> None:
    if psycopg is None:
        pytest.skip("psycopg not installed")


def _get_server_version(port: int) -> str:
    conn = psycopg.connect(conninfo=_pg_dsn(port), autocommit=True)
    try:
        cur = conn.execute("SHOW server_version_num")
        v = cur.fetchone()[0]
        # server_version_num is e.g. 160004 for PG 16.0.4
        major = int(v) // 10000
        return str(major)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

matrix_versions = ["14", "15", "16", "17"]


def _matrix_versions() -> list[str]:
    raw = os.environ.get("DBK_PG_DOCKER_VERSIONS", "14,15,16,17")
    return [v.strip() for v in raw.split(",") if v.strip() in matrix_versions]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_pg_containers: list[tuple[str, int, str]] = []  # (version, port, container_name)


def _start_matrix_containers() -> list[tuple[str, int, str]]:
    """Start all configured PG containers and return (version, port, name)."""
    versions = _matrix_versions()
    started = []
    for version in versions:
        port = PG_PORTS.get(version)
        if port is None:
            continue
        suffix = f"dbk{int(time.time()) % 100000:05d}"
        name = f"dbk-pg-{version}-{suffix}"
        result = subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", name,
                "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}",
                "-e", "POSTGRES_DB=postgres",
                "-p", f"{port}:5432",
                f"postgres:{version}",
            ],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            # Try to clean up any already-started containers
            for _, _, n in started:
                subprocess.run(["docker", "rm", "-f", n],
                               capture_output=True, check=False)
            raise RuntimeError(f"docker run failed for PG {version}: {result.stderr}")
        started.append((version, port, name))
    return started


def _stop_matrix_containers() -> None:
    for _, _, name in _pg_containers:
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, check=False)


@pytest.fixture(scope="module", autouse=True)
def pg_matrix_containers():
    if os.environ.get("DBK_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set DBK_RUN_DOCKER_TESTS=1 to run docker integration tests.")
    _skip_if_no_docker()
    _skip_if_no_psycopg()

    containers = _start_matrix_containers()
    # Wait for all to be ready
    for version, port, name in containers:
        if not _wait_for_pg(port, timeout=60):
            _stop_matrix_containers()
            pytest.fail(f"PG {version} did not become ready in time")

    yield containers

    _stop_matrix_containers()


# ---------------------------------------------------------------------------
# Tests: Health
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,port", [
    pytest.param(v, PG_PORTS[v], id=f"pg-{v}")
    for v in ["14", "15", "16", "17"]
    if v in _matrix_versions()
])
def test_health_check(version: str, port: int, pg_matrix_containers) -> None:
    dsn = _pg_dsn(port)
    report = collect_pg_health(dsn)
    assert report.ok, f"PG {version} health check failed: {report.error}"


# ---------------------------------------------------------------------------
# Tests: Version Detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,port", [
    pytest.param(v, PG_PORTS[v], id=f"pg-{v}")
    for v in ["14", "15", "16", "17"]
    if v in _matrix_versions()
])
def test_version_detection(version: str, port: int, pg_matrix_containers) -> None:
    detected = _get_server_version(port)
    assert detected == version, f"Expected PG {version}, detected {detected}"


@pytest.mark.parametrize("version", ["14", "15", "16", "17"])
def test_pg_features_for_version(version: str) -> None:
    version_int = int(version) * 100000  # map "14" -> 140000
    features = _pg_features_for_version(version_int)
    assert isinstance(features, set)
    assert "pg_stat_statements" in features
    assert "pg_stat_bgwriter" in features
    assert "pg_stat_activity" in features
    # pg_stat_io only present in PG 16+
    if int(version) >= 16:
        assert "pg_stat_io" in features


# ---------------------------------------------------------------------------
# Tests: Runtime Metrics Collection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,port", [
    pytest.param(v, PG_PORTS[v], id=f"pg-{v}")
    for v in ["14", "15", "16", "17"]
    if v in _matrix_versions()
])
def test_collect_runtime_metrics(version: str, port: int, pg_matrix_containers) -> None:
    dsn = _pg_dsn(port)
    result = collect_pg_runtime_metrics(instance=f"pg-{version}", dsn=dsn)
    assert len(result.events) >= 6, f"PG {version}: expected >=6 events, got {len(result.events)}"
    metrics = {ev.metric for ev in result.events}
    assert "query.p95_latency_ms" in metrics
    assert "buffer.hit_ratio_pct" in metrics
    # Verify all events have numeric values
    for ev in result.events:
        assert isinstance(ev.value, float), f"PG {version}: non-float value for {ev.metric}"


@pytest.mark.parametrize("version,port", [
    pytest.param(v, PG_PORTS[v], id=f"pg-{v}")
    for v in ["14", "15", "16", "17"]
    if v in _matrix_versions()
])
def test_metrics_storable_via_sdk(version: str, port: int, pg_matrix_containers) -> None:
    # Verify SDK store_metric works with pgstat data
    client = DBKClient({"pg_dsn": _pg_dsn(port), "provider": "mock", "model": "mock"})
    result = client.collect_metrics(instance=f"pg-sdk-{version}", source="pgstat")
    assert result["collected"] >= 6


# ---------------------------------------------------------------------------
# Tests: Lock Contention Diagnostics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,port", [
    pytest.param(v, PG_PORTS[v], id=f"pg-{v}")
    for v in ["14", "15", "16", "17"]
    if v in _matrix_versions()
])
def test_lock_contention_diagnosis(version: str, port: int, pg_matrix_containers) -> None:
    dsn = _pg_dsn(port)
    diagnosis = diagnose_lock_contention(dsn=dsn, instance=f"pg-{version}")
    assert "verdict" in diagnosis
    assert "findings" in diagnosis
    assert "diagnostic_queries" in diagnosis
    assert isinstance(diagnosis["findings"], list)
    assert isinstance(diagnosis["diagnostic_queries"], list)


# ---------------------------------------------------------------------------
# Tests: Replication Bottleneck Diagnostics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,port", [
    pytest.param(v, PG_PORTS[v], id=f"pg-{v}")
    for v in ["14", "15", "16", "17"]
    if v in _matrix_versions()
])
def test_replication_bottleneck_diagnosis(version: str, port: int, pg_matrix_containers) -> None:
    dsn = _pg_dsn(port)
    diagnosis = diagnose_replication_bottleneck(dsn=dsn, instance=f"pg-{version}")
    assert "verdict" in diagnosis
    assert "findings" in diagnosis
    assert "diagnostic_queries" in diagnosis
    assert isinstance(diagnosis["findings"], list)
    assert isinstance(diagnosis["diagnostic_queries"], list)


# ---------------------------------------------------------------------------
# Tests: SDK Full Pipeline (health + collect + diagnose + cleanup)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,port", [
    pytest.param(v, PG_PORTS[v], id=f"pg-{v}")
    for v in ["14", "15", "16", "17"]
    if v in _matrix_versions()
])
def test_sdk_full_pipeline(version: str, port: int, pg_matrix_containers) -> None:
    dsn = _pg_dsn(port)
    client = DBKClient({
        "pg_dsn": dsn,
        "provider": "mock",
        "model": "mock",
    })

    # Health check
    health = client.health_check(source="pgstat", dsn=dsn)
    assert "ok" in health or "degraded" in health

    # Collect
    coll = client.collect_metrics(instance=f"pg-pipe-{version}", source="pgstat", dsn=dsn)
    assert coll["collected"] >= 6

    # Diagnose
    diag = client.diagnose_incident(instance=f"pg-pipe-{version}", auto_trace=False)
    assert "verdict" in diag

    # Cleanup dry-run
    cleanup = client.cleanup_data(older_than_hours=1.0, dry_run=True)
    assert "ok" in cleanup


# ---------------------------------------------------------------------------
# Tests: Config Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version,port", [
    pytest.param(v, PG_PORTS[v], id=f"pg-{v}")
    for v in ["14", "15", "16", "17"]
    if v in _matrix_versions()
])
def test_config_validation_with_pg(version: str, port: int, pg_matrix_containers) -> None:
    client = DBKClient({
        "pg_dsn": _pg_dsn(port),
        "provider": "mock",
        "model": "mock",
    })
    result = client.validate_config()
    assert result["ok"] is True
    assert result["problems"] == []
