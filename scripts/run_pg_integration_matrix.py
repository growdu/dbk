#!/usr/bin/env python3
"""Run the DBK PostgreSQL integration test matrix.

Usage::

    # Full matrix (requires Docker + internet)
    python scripts/run_pg_integration_matrix.py

    # Specific versions
    DBK_PG_DOCKER_VERSIONS=15,16 python scripts/run_pg_integration_matrix.py

    # Dry run (list what would be tested)
    python scripts/run_pg_integration_matrix.py --dry-run

    # Tear down only
    python scripts/run_pg_integration_matrix.py --down

The script:
  1. Starts PG 14/15/16/17 containers via docker-compose
  2. Waits for all to be healthy
  3. Runs the integration test suite
  4. Tears down containers on exit
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "integration" / "docker-compose.yml"


def _run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    print(f"[runner] {' '.join(cmd)}")
    kwargs = {"check": check}
    if capture:
        kwargs["capture_output"] = True
        kwargs["text"] = True
    return subprocess.run(cmd, **kwargs)  # type: ignore[arg-type]


def up(versions: list[str]) -> None:
    env = os.environ.copy()
    env["DBK_PG_DOCKER_VERSIONS"] = ",".join(versions)
    _run(["docker-compose", "-f", str(COMPOSE_FILE), "up", "-d"], check=True, capture=False)


def down() -> None:
    _run(["docker-compose", "-f", str(COMPOSE_FILE), "down"], check=False, capture=True)


def wait_healthy(versions: list[str], timeout: int = 90) -> dict[str, bool]:
    ports = {"14": 5433, "15": 5434, "16": 5435, "17": 5436}
    deadline = time.time() + timeout
    ready: dict[str, bool] = {v: False for v in versions}

    import socket
    import dbk.pg_collectors as pg_mod

    while time.time() < deadline:
        all_ready = True
        for v in versions:
            if ready[v]:
                continue
            port = ports.get(v)
            if port is None:
                continue
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                sock.close()
                # Try a quick PG query
                try:
                    import psycopg
                    conn = psycopg.connect(
                        f"postgresql://postgres:dbk_secret@127.0.0.1:{port}/postgres",
                        connect_timeout=3,
                        autocommit=True,
                    )
                    conn.close()
                    ready[v] = True
                    print(f"[runner] PG {v} is ready on port {port}")
                except Exception as e:
                    print(f"[runner] PG {v} port open but not ready: {e}")
                    all_ready = False
            except OSError:
                all_ready = False
        if all_ready:
            break
        time.sleep(2)

    return ready


def run_tests() -> bool:
    env = os.environ.copy()
    env["DBK_RUN_DOCKER_TESTS"] = "1"
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "tests/test_pg_integration_matrix.py",
            "-v", "--tb=short",
            "-x",
        ],
        cwd=str(ROOT),
        env=env,
    )
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="DBK PG integration matrix runner")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without running")
    parser.add_argument("--down", action="store_true", help="Tear down containers and exit")
    parser.add_argument(
        "--versions",
        default=os.environ.get("DBK_PG_DOCKER_VERSIONS", "14,15,16,17"),
        help="PG versions to test (default: 14,15,16,17)",
    )
    args = parser.parse_args()

    versions = [v.strip() for v in args.versions.split(",") if v.strip()]

    if args.dry_run:
        print(f"Would test PG versions: {versions}")
        print(f"Compose file: {COMPOSE_FILE}")
        print(f"Test file: tests/test_pg_integration_matrix.py")
        return

    if args.down:
        print("[runner] Tearing down containers...")
        down()
        print("[runner] Done.")
        return

    print(f"[runner] Starting PG integration matrix for versions: {versions}")
    print(f"[runner] Using compose file: {COMPOSE_FILE}")

    try:
        print("[runner] Bringing up containers...")
        up(versions)

        print("[runner] Waiting for containers to be healthy...")
        ready = wait_healthy(versions)
        if not all(ready.values()):
            missing = [v for v, r in ready.items() if not r]
            print(f"[runner] WARNING: containers not ready after timeout: {missing}")
            print("[runner] Proceeding with tests anyway...")

        print("[runner] Running integration tests...")
        ok = run_tests()
        sys.exit(0 if ok else 1)
    finally:
        print("[runner] Tearing down containers...")
        down()
        print("[runner] Done.")


if __name__ == "__main__":
    main()
