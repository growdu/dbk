"""Integration tests for dbk alert rules CLI commands."""

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
        code, out = _run(["alert", "rules", "list"])
        assert code == 0, out
        data = json.loads(out)
        assert "rules" in data
        assert "count" in data
        assert data["count"] == 9
        names = {r["name"] for r in data["rules"]}
        assert "query_latency_high" in names
        assert "connection_high" in names
        assert "replication_lag" in names

    def test_list_rules_are_valid_json(self) -> None:
        code, out = _run(["alert", "rules", "list"])
        assert code == 0
        # Should not raise
        data = json.loads(out)
        assert len(data["rules"]) > 0


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
        data = json.loads(out)
        rule = data["added"]
        assert rule["name"] == "test_cpu_high"
        assert rule["metric"] == "cpu_usage"
        assert rule["operator"] == "gt"
        assert rule["threshold"] == 90.0
        assert rule["severity"] == "critical"

    def test_add_rule_with_all_options(self) -> None:
        code, out = _run([
            "alert", "rules", "add",
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
        rule = data["added"]
        assert rule["description"] == "Too many lock waits"
        assert rule["instance"] == "pg-main"
        assert rule["minimum_duration_sec"] == 30
        assert rule["cooldown_sec"] == 600


class TestAlertRulesExport:
    def test_export_rules_to_json(self, tmp_path: Path) -> None:
        out_path = tmp_path / "exported_rules.json"
        code, out = _run([
            "alert", "rules", "export",
            "--path", str(out_path),
            "--include-builtin",
        ])
        assert code == 0, out
        data = json.loads(out)
        assert data["count"] == 9
        assert out_path.exists()
        saved = json.loads(out_path.read_text())
        assert len(saved["rules"]) == 9


class TestAlertRulesValidate:
    def test_validate_nonexistent_file_returns_error(self) -> None:
        code, out = _run(["alert", "rules", "validate", "--rules-path", "/nonexistent/rules.json"])
        assert code == 2, out
        data = json.loads(out)
        assert data["valid"] is False
        assert "error" in data
