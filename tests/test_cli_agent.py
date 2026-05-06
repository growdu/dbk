"""Integration tests for dbk agent CLI subcommands."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _run(args: list[str]) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "dbk"] + args,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return result.returncode, result.stdout + result.stderr


class TestAgentTools:
    def test_tools_shows_registered_tools(self) -> None:
        code, out = _run(["agent", "tools"])
        assert code == 0, out
        data = json.loads(out)
        assert "tools" in data
        assert "count" in data
        assert data["count"] == 12
        names = {t["name"] for t in data["tools"]}
        assert "collect_metrics" in names
        assert "query_metrics" in names
        assert "diagnose_incident" in names
        assert "run_trace" in names
        assert "cleanup_data" in names

    def test_tools_have_required_schema_fields(self) -> None:
        code, out = _run(["agent", "tools"])
        assert code == 0
        data = json.loads(out)
        for tool in data["tools"]:
            assert "name" in tool
            assert "description" in tool
            assert "parameters" in tool
            assert tool["parameters"]["type"] == "object"


class TestAgentSessions:
    def test_sessions_returns_list(self) -> None:
        code, out = _run(["agent", "sessions"])
        assert code == 0, out
        data = json.loads(out)
        assert "sessions" in data
        assert "count" in data
        assert isinstance(data["count"], int)

    def test_sessions_clear_requires_flag(self) -> None:
        # --clear without --all or --session should fail
        code, out = _run(["agent", "sessions", "--clear"])
        # Should fail because neither --all nor --session given
        assert code == 2, out

    def test_sessions_clear_all(self) -> None:
        code, out = _run(["agent", "sessions", "--clear", "--all"])
        assert code == 0, out
        data = json.loads(out)
        assert data["deleted_all"] is True
        assert "count" in data


class TestAgentInfo:
    def test_info_returns_provider_info(self) -> None:
        code, out = _run(["agent", "info"])
        assert code == 0, out
        data = json.loads(out)
        assert "provider" in data or "model" in data or "configured" in data
