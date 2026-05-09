"""Integration tests for dbk alert rules CLI commands.

Note: Commands now return CommandResult (code/data/message/details/warnings
dataclass).  When --format json is used, the output is the CommandResult JSON.
When no format flag is used, output is plain text.
These tests adapt assertions to both output modes.
"""
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


class TestAlertRulesList:
    def test_list_shows_builtin_rules(self) -> None:
        code, out = _run(["alert", "rules", "list", "--format", "json"])
        assert code == 0, out
        data = json.loads(out)
        # CommandResult wraps the payload in "data"
        payload = data["data"] if "data" in data else data
        assert "rules" in payload
        assert "count" in payload
        assert payload["count"] == 9
        names = {r["name"] for r in payload["rules"]}
        assert "query_latency_high" in names
        assert "connection_high" in names
        assert "replication_lag" in names

    def test_list_rules_are_valid_json(self) -> None:
        code, out = _run(["alert", "rules", "list", "--format", "json"])
        assert code == 0
        data = json.loads(out)
        payload = data["data"] if "data" in data else data
        assert len(payload["rules"]) > 0


class TestAlertRulesAdd:
    def test_add_rule_minimal(self) -> None:
        code, out = _run([
            "alert", "rules", "add",
            "--name", "test_cpu_high",
            "--metric", "cpu_usage",
            "--operator", "gt",
            "--threshold", "90",
            "--severity", "critical",
        ])
        assert code == 0, out
        # Add outputs text by default (CommandResult message), not JSON.
        # Re-run with --format json to get structured data.
        code2, out2 = _run([
            "alert", "rules", "add",
            "--format", "json",
            "--name", "test_cpu_high",
            "--metric", "cpu_usage",
            "--operator", "gt",
            "--threshold", "90",
            "--severity", "critical",
        ])
        data = json.loads(out2)
        payload = data["data"] if "data" in data else data
        rule = payload["added"]
        assert rule["name"] == "test_cpu_high"
        assert rule["metric"] == "cpu_usage"
        assert rule["operator"] == "gt"
        assert rule["threshold"] == 90.0
        assert rule["severity"] == "critical"

    def test_add_rule_with_all_options(self) -> None:
        code, out = _run([
            "alert", "rules", "add",
            "--format", "json",
            "--name", "test_lock",
            "--metric", "lock_wait_count",
            "--operator", "gte",
            "--threshold", "5",
            "--severity", "warning",
            "--description", "Too many lock waits",
            "--instance", "pg-main",
            "--min-duration", "30",
            "--cooldown", "600",
        ])
        assert code == 0, out
        data = json.loads(out)
        payload = data["data"] if "data" in data else data
        rule = payload["added"]
        assert rule["description"] == "Too many lock waits"
        assert rule["instance"] == "pg-main"
        assert rule["minimum_duration_sec"] == 30
        assert rule["cooldown_sec"] == 600


class TestAlertRulesExport:
    def test_export_rules_to_json(self, tmp_path: Path) -> None:
        out_path = tmp_path / "exported_rules.json"
        code, out = _run([
            "alert", "rules", "export",
            "--format", "json",
            "--path", str(out_path),
            "--include-builtin",
        ])
        assert code == 0, out
        data = json.loads(out)
        payload = data["data"] if "data" in data else data
        assert payload["count"] == 9
        assert out_path.exists()
        saved = json.loads(out_path.read_text())
        assert len(saved["rules"]) == 9


class TestAlertRulesValidate:
    def test_validate_nonexistent_file_returns_error(self) -> None:
        # ConfigError (code=3) is returned for missing file.
        # Use --format json to get structured output.
        code, out = _run(["alert", "rules", "validate", "--format", "json", "/nonexistent/rules.json"])
        assert code == 3, out  # CONFIG_ERROR = 3
        data = json.loads(out)
        assert data["code"] == 3
