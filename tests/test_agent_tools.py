"""Tests for agent tool functions: diagnose_incident, run_trace, etc."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dbk.agent.tools import (
    tool_cleanup_data,
    tool_diagnose_incident,
    tool_health_check,
    tool_query_metrics,
    tool_run_trace,
    tool_start_collector_daemon,
    tool_stop_collector_daemon,
)


class TestToolDiagnoseIncident:
    def test_diagnose_returns_dict(self) -> None:
        mock_result = MagicMock()
        mock_result.verdict = "healthy"
        mock_result.findings = []
        mock_result.evidence_bundle = Path("/tmp/ev")
        mock_result.trace_summary = None

        # Patch at source module (lazy import inside tool function)
        with patch("dbk.diagnose.diagnose_latency_incident", return_value=mock_result):
            with patch("dbk.agent.tools.artifacts_root", return_value=Path("/tmp")):
                with patch("dbk.agent.tools._store") as mock_store:
                    mock_store.return_value = MagicMock()
                    result = tool_diagnose_incident(instance="pg-01", task_id="t1")

        assert isinstance(result, dict)
        assert result["verdict"] == "healthy"

    def test_diagnose_with_auto_trace(self) -> None:
        mock_result = MagicMock()
        mock_result.verdict = "degraded"
        mock_result.findings = ["high latency"]
        mock_result.evidence_bundle = Path("/tmp/ev")
        mock_result.trace_summary = Path("/tmp/trace.json")

        with patch("dbk.diagnose.diagnose_latency_incident", return_value=mock_result):
            with patch("dbk.agent.tools.artifacts_root", return_value=Path("/tmp")):
                with patch("dbk.agent.tools._store") as mock_store:
                    mock_store.return_value = MagicMock()
                    result = tool_diagnose_incident(
                        instance="pg-01", task_id="t1", auto_trace=True,
                    )

        assert isinstance(result, dict)
        assert "findings" in result


class TestToolRunTrace:
    def test_run_trace_without_execute(self) -> None:
        mock_artifact = MagicMock()
        mock_artifact.summary_json = {}

        mock_result = MagicMock()
        mock_result.stdout_path = Path("/tmp/trace.out")
        mock_result.summary_path = Path("/tmp/trace.json")
        mock_result.artifact = mock_artifact

        # Patch at source module where run_trace_profile is defined
        with patch("dbk.tracing.run_trace_profile", return_value=mock_result):
            with patch("dbk.agent.tools.artifacts_root", return_value=Path("/tmp")):
                result = tool_run_trace(task_id="t1", profile="cpu-hotpath", execute=False)

        assert isinstance(result, dict)
        assert result["profile"] == "cpu-hotpath"
        assert result["task_id"] == "t1"

    def test_run_trace_with_execute(self) -> None:
        mock_artifact = MagicMock()
        mock_artifact.summary_json = {"events": 10}

        mock_result = MagicMock()
        mock_result.stdout_path = Path("/tmp/trace.out")
        mock_result.summary_path = Path("/tmp/trace.json")
        mock_result.artifact = mock_artifact

        with patch("dbk.tracing.run_trace_profile", return_value=mock_result):
            with patch("dbk.agent.tools.artifacts_root", return_value=Path("/tmp")):
                result = tool_run_trace(
                    task_id="t1", profile="io-latency", execute=True, duration_sec=1,
                )

        assert isinstance(result, dict)

    def test_run_trace_unsupported_profile_raises(self) -> None:
        with patch("dbk.tracing.run_trace_profile") as mock_fn:
            mock_fn.side_effect = ValueError("Unsupported profile: unknown")
            with patch("dbk.agent.tools.artifacts_root", return_value=Path("/tmp")):
                with pytest.raises(ValueError, match="Unsupported profile"):
                    tool_run_trace(task_id="t1", profile="unknown", execute=False)


class TestToolHealthCheck:
    def test_health_check_mock(self) -> None:
        result = tool_health_check(source="mock")
        assert isinstance(result, dict)
        assert result["ok"] is True
        assert result["degraded"] is False

    def test_health_check_mock_with_dsn(self) -> None:
        result = tool_health_check(source="mock", dsn="postgresql://localhost/pg")
        assert isinstance(result, dict)
        assert result["ok"] is True


class TestToolQueryMetrics:
    def test_query_metrics_with_mock_store(self) -> None:
        # Patch _store so no real DB is needed
        mock_store = MagicMock()
        mock_store.query_latest_metric.return_value = iter([])

        with patch("dbk.agent.tools._store", return_value=mock_store):
            result = tool_query_metrics(metric="cpu_usage")

        assert isinstance(result, dict)
        assert result["metric"] == "cpu_usage"
        assert result["mode"] == "latest"
        mock_store.query_latest_metric.assert_called_once()

    def test_query_metrics_with_instance_and_limit(self) -> None:
        mock_store = MagicMock()
        mock_store.query_latest_metric.return_value = iter([
            {"ts": "2026-04-30T00:00:00Z", "value": 45.0, "labels_json": "{}"},
        ])

        with patch("dbk.agent.tools._store", return_value=mock_store):
            result = tool_query_metrics(metric="cpu_usage", instance="pg-01", limit=5)

        assert isinstance(result, dict)
        assert result["metric"] == "cpu_usage"
        assert result["mode"] == "latest"


class TestToolCleanupData:
    def test_cleanup_data_dry_run(self) -> None:
        mock_summary = MagicMock()
        mock_summary.to_dict.return_value = {
            "cleaned_events": 0, "cleaned_traces": 0,
            "freed_bytes": 0, "dry_run": True,
        }

        # Patch at source module where cleanup_runtime_data is defined
        with patch("dbk.runtime_cleanup.cleanup_runtime_data", return_value=mock_summary):
            with patch("dbk.agent.tools._store") as mock_store_fn:
                mock_store_fn.return_value = MagicMock()
                result = tool_cleanup_data(older_than_hours=24, dry_run=True)

        assert isinstance(result, dict)
        assert result["dry_run"] is True


class TestToolCollectorDaemon:
    def test_start_daemon(self) -> None:
        mock_state = MagicMock()
        mock_state.pid = 99999
        mock_state.instance = "pg-test-01"

        # start_daemon is imported inside tool_start_collector_daemon from collector_daemon
        with patch("dbk.collector_daemon.start_daemon", return_value=mock_state):
            result = tool_start_collector_daemon(instance="pg-test-01", source="mock")

        assert isinstance(result, dict)
        assert result["started"] is True
        assert result["pid"] == 99999

    def test_stop_daemon_by_instance(self) -> None:
        with patch("dbk.collector_daemon.stop_daemon", return_value={"stopped": True}):
            result = tool_stop_collector_daemon(instance="pg-test-01")

        assert isinstance(result, dict)
        assert result["stopped"] is True

    def test_stop_all_daemons(self) -> None:
        with patch("dbk.collector_daemon.stop_all_daemons", return_value={"stopped": 3}):
            result = tool_stop_collector_daemon(all_instances=True)

        assert isinstance(result, dict)

    def test_stop_requires_instance_or_all(self) -> None:
        with pytest.raises(ValueError, match="Must provide"):
            tool_stop_collector_daemon()
