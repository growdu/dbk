"""Tests for the alerting module."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dbk.alerting import (
    Alert,
    AlertEngine,
    AlertEvent,
    AlertNotifier,
    AlertPrometheusExporter,
    AlertRule,
    AlertStore,
    AlertState,
    CompositeNotifier,
    LogNotifier,
    Severity,
    WebhookNotifier,
    load_rules,
)
from dbk.alerting.models import AlertRule as AlertRuleModel


# ---------------------------------------------------------------------------
# AlertRule
# ---------------------------------------------------------------------------

class TestAlertRule:
    def test_evaluate_gt(self) -> None:
        rule = AlertRule(
            name="test",
            metric="cpu.usage",
            operator="gt",
            threshold=80.0,
            severity=Severity.CRITICAL,
            description="CPU too high",
        )
        assert rule.evaluate(95.0) is True
        assert rule.evaluate(50.0) is False
        assert rule.evaluate(80.0) is False  # boundary: gt is strictly greater

    def test_evaluate_gte(self) -> None:
        rule = AlertRule(
            name="test", metric="x", operator="gte", threshold=80.0,
            severity=Severity.WARNING, description="",
        )
        assert rule.evaluate(80.0) is True
        assert rule.evaluate(80.1) is True
        assert rule.evaluate(79.9) is False

    def test_evaluate_lt(self) -> None:
        rule = AlertRule(
            name="test", metric="x", operator="lt", threshold=10.0,
            severity=Severity.INFO, description="",
        )
        assert rule.evaluate(5.0) is True
        assert rule.evaluate(9.9) is True
        assert rule.evaluate(10.0) is False

    def test_evaluate_lte(self) -> None:
        rule = AlertRule(
            name="test", metric="x", operator="lte", threshold=10.0,
            severity=Severity.INFO, description="",
        )
        assert rule.evaluate(10.0) is True
        assert rule.evaluate(10.1) is False

    def test_evaluate_eq(self) -> None:
        rule = AlertRule(
            name="test", metric="x", operator="eq", threshold=42.0,
            severity=Severity.INFO, description="",
        )
        assert rule.evaluate(42.0) is True
        assert rule.evaluate(42.0001) is False

    def test_evaluate_unknown_operator(self) -> None:
        rule = AlertRule(
            name="test", metric="x", operator="unknown", threshold=0.0,
            severity=Severity.INFO, description="",
        )
        assert rule.evaluate(0.0) is False
        assert rule.evaluate(1.0) is False

    def test_to_dict_from_dict_roundtrip(self) -> None:
        original = AlertRule(
            name="cpu_high",
            metric="cpu.usage",
            operator="gt",
            threshold=90.0,
            severity=Severity.CRITICAL,
            description="CPU usage critical",
            instance="pg-main-01",
            minimum_duration_sec=30,
            cooldown_sec=600,
            labels={"env": "prod", "datacenter": "us-east-1"},
        )
        d = original.to_dict()
        restored = AlertRule.from_dict(d)
        assert restored.name == original.name
        assert restored.metric == original.metric
        assert restored.threshold == original.threshold
        assert restored.severity == original.severity
        assert restored.labels == original.labels
        assert restored.cooldown_sec == original.cooldown_sec


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------

class TestAlert:
    def test_to_dict_from_dict_roundtrip(self) -> None:
        original = Alert(
            id="a1b2c3",
            rule_name="high_cpu",
            metric="cpu.usage",
            value=95.5,
            threshold=80.0,
            operator="gt",
            severity=Severity.CRITICAL,
            state=AlertState.FIRING,
            instance="pg-main-01",
            description="CPU critical",
            fired_at="2026-04-30T03:00:00Z",
            labels={"env": "prod"},
            annotations={"dashboard": "https://..."},
        )
        d = original.to_dict()
        restored = Alert.from_dict(d)
        assert restored.id == original.id
        assert restored.value == original.value
        assert restored.state == original.state
        assert restored.labels == original.labels


# ---------------------------------------------------------------------------
# AlertEngine
# ---------------------------------------------------------------------------

class TestAlertEngine:
    def test_fire_on_violation(self) -> None:
        rule = AlertRule(
            name="high_cpu",
            metric="cpu.usage",
            operator="gt",
            threshold=80.0,
            severity=Severity.CRITICAL,
            description="CPU too high",
        )
        engine = AlertEngine(rules=[rule])
        events = engine.evaluate("cpu.usage", 95.0, instance="pg-main-01")
        assert len(events) == 1
        assert events[0].type == "firing"
        assert events[0].alert.severity == Severity.CRITICAL
        assert events[0].alert.value == 95.0
        assert events[0].alert.threshold == 80.0

    def test_no_event_when_within_threshold(self) -> None:
        rule = AlertRule(
            name="high_cpu",
            metric="cpu.usage",
            operator="gt",
            threshold=80.0,
            severity=Severity.CRITICAL,
            description="",
        )
        engine = AlertEngine(rules=[rule])
        events = engine.evaluate("cpu.usage", 50.0, instance="pg-main-01")
        assert len(events) == 0
        assert engine.get_active_count() == 0

    def test_resolve_when_condition_clears(self) -> None:
        rule = AlertRule(
            name="high_cpu",
            metric="cpu.usage",
            operator="gt",
            threshold=80.0,
            severity=Severity.CRITICAL,
            description="",
        )
        engine = AlertEngine(rules=[rule])
        # Fire
        events = engine.evaluate("cpu.usage", 95.0, instance="pg-main-01")
        assert len(events) == 1
        assert events[0].type == "firing"
        # Clear
        events2 = engine.evaluate("cpu.usage", 50.0, instance="pg-main-01")
        assert len(events2) == 1
        assert events2[0].type == "resolved"
        assert engine.get_active_count() == 0

    def test_instance_filter(self) -> None:
        rule = AlertRule(
            name="high_cpu",
            metric="cpu.usage",
            operator="gt",
            threshold=80.0,
            severity=Severity.CRITICAL,
            description="",
            instance="pg-main-01",
        )
        engine = AlertEngine(rules=[rule])
        # Should fire for matching instance
        events = engine.evaluate("cpu.usage", 95.0, instance="pg-main-01")
        assert len(events) == 1
        # Should not fire for different instance
        events2 = engine.evaluate("cpu.usage", 95.0, instance="pg-replica-01")
        assert len(events2) == 0

    def test_batch_evaluation(self) -> None:
        rules = [
            AlertRule(
                name="high_cpu",
                metric="cpu.usage",
                operator="gt",
                threshold=80.0,
                severity=Severity.CRITICAL,
                description="",
            ),
            AlertRule(
                name="low_memory",
                metric="mem.available_pct",
                operator="lt",
                threshold=20.0,
                severity=Severity.WARNING,
                description="",
            ),
        ]
        engine = AlertEngine(rules=rules)
        metrics = [
            {"metric": "cpu.usage", "value": 95.0, "instance": "pg-main-01"},
            {"metric": "mem.available_pct", "value": 10.0, "instance": "pg-main-01"},
        ]
        events = engine.evaluate_batch(metrics)
        assert len(events) == 2
        assert engine.get_active_count() == 2

    def test_listener_receives_events(self) -> None:
        rule = AlertRule(
            name="test", metric="x", operator="gt", threshold=0.0,
            severity=Severity.INFO, description="",
        )
        engine = AlertEngine(rules=[rule])
        received: list[AlertEvent] = []

        def listener(event: AlertEvent) -> None:
            received.append(event)

        engine.add_listener(listener)
        engine.evaluate("x", 1.0, instance="inst")
        assert len(received) == 1
        assert received[0].type == "firing"

    def test_remove_listener(self) -> None:
        rule = AlertRule(
            name="test", metric="x", operator="gt", threshold=0.0,
            severity=Severity.INFO, description="",
        )
        engine = AlertEngine(rules=[rule])
        received: list[AlertEvent] = []

        def listener(event: AlertEvent) -> None:
            received.append(event)

        engine.add_listener(listener)
        engine.remove_listener(listener)
        engine.evaluate("x", 1.0, instance="inst")
        assert len(received) == 0

    def test_get_firing_count_by_severity(self) -> None:
        rules = [
            AlertRule(
                name="c1", metric="a", operator="gt", threshold=0.0,
                severity=Severity.CRITICAL, description="",
            ),
            AlertRule(
                name="c2", metric="b", operator="gt", threshold=0.0,
                severity=Severity.CRITICAL, description="",
            ),
            AlertRule(
                name="w1", metric="c", operator="gt", threshold=0.0,
                severity=Severity.WARNING, description="",
            ),
        ]
        engine = AlertEngine(rules=rules)
        engine.evaluate("a", 1.0, instance="inst")
        engine.evaluate("b", 1.0, instance="inst")
        engine.evaluate("c", 1.0, instance="inst")
        counts = engine.get_firing_count_by_severity()
        assert counts[Severity.CRITICAL] == 2
        assert counts[Severity.WARNING] == 1
        assert counts[Severity.INFO] == 0

    def test_update_rules(self) -> None:
        engine = AlertEngine(rules=[])
        rule = AlertRule(
            name="test", metric="x", operator="gt", threshold=50.0,
            severity=Severity.INFO, description="",
        )
        engine.update_rules([rule])
        events = engine.evaluate("x", 99.0, instance="inst")
        assert len(events) == 1


# ---------------------------------------------------------------------------
# load_rules
# ---------------------------------------------------------------------------

class TestLoadRules:
    def test_load_valid_rules_file(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "rules.json"
        rules_file.write_text(
            json.dumps({
                "rules": [
                    {
                        "name": "high_cpu",
                        "metric": "cpu.usage",
                        "operator": "gt",
                        "threshold": 80.0,
                        "severity": "critical",
                        "description": "CPU too high",
                    },
                    {
                        "name": "low_mem",
                        "metric": "mem.available_pct",
                        "operator": "lt",
                        "threshold": 20.0,
                        "severity": "warning",
                        "description": "Memory low",
                    },
                ]
            }),
            encoding="utf-8",
        )
        rules = load_rules(rules_file)
        assert len(rules) == 2
        assert rules[0].name == "high_cpu"
        assert rules[0].severity == Severity.CRITICAL
        assert rules[1].operator == "lt"

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Rules file not found"):
            load_rules(tmp_path / "nonexistent.json")

    def test_load_malformed_json_raises(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "bad.json"
        rules_file.write_text("not json", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_rules(rules_file)

    def test_load_non_list_rules_raises(self, tmp_path: Path) -> None:
        rules_file = tmp_path / "bad.json"
        rules_file.write_text(json.dumps({"rules": "not a list"}), encoding="utf-8")
        with pytest.raises(ValueError, match="'rules' must be a list"):
            load_rules(rules_file)


# ---------------------------------------------------------------------------
# AlertStore
# ---------------------------------------------------------------------------

class TestAlertStore:
    def test_init_schema(self, tmp_path: Path) -> None:
        store = AlertStore(tmp_path / "alerts.sqlite")
        store.init_schema()
        # Should not raise
        store.init_schema()

    def test_insert_and_query_alert(self, tmp_path: Path) -> None:
        store = AlertStore(tmp_path / "alerts.sqlite")
        store.init_schema()
        alert = Alert(
            id="alert-001",
            rule_name="high_cpu",
            metric="cpu.usage",
            value=95.0,
            threshold=80.0,
            operator="gt",
            severity=Severity.CRITICAL,
            state=AlertState.FIRING,
            instance="pg-main-01",
            description="CPU critical",
            fired_at="2026-04-30T03:00:00+00:00",
            labels={"env": "prod"},
        )
        store.insert_alert(alert)
        firing = store.query_firing_alerts()
        assert len(firing) == 1
        assert firing[0].id == "alert-001"
        assert firing[0].severity == Severity.CRITICAL

    def test_update_alert_state_resolved(self, tmp_path: Path) -> None:
        store = AlertStore(tmp_path / "alerts.sqlite")
        store.init_schema()
        alert = Alert(
            id="alert-002",
            rule_name="high_cpu",
            metric="cpu.usage",
            value=95.0,
            threshold=80.0,
            operator="gt",
            severity=Severity.CRITICAL,
            state=AlertState.FIRING,
            instance="pg-main-01",
            description="",
            fired_at="2026-04-30T03:00:00+00:00",
        )
        store.insert_alert(alert)
        store.update_alert_state("alert-002", AlertState.RESOLVED, resolved_at="2026-04-30T04:00:00+00:00")
        firing = store.query_firing_alerts()
        assert len(firing) == 0
        alerts = store.query_alerts()
        assert len(alerts) == 1
        assert alerts[0].state == AlertState.RESOLVED

    def test_query_alerts_filters(self, tmp_path: Path) -> None:
        store = AlertStore(tmp_path / "alerts.sqlite")
        store.init_schema()
        # Use dynamic timestamps relative to now so tests are not date-sensitive.
        now = datetime.now(tz=timezone.utc)
        alert1 = Alert(
            id="a1", rule_name="r1", metric="x", value=1.0, threshold=0.0,
            operator="gt", severity=Severity.CRITICAL, state=AlertState.FIRING,
            instance="inst1", description="",
            fired_at=(now - timedelta(hours=2)).replace(microsecond=0).isoformat(),
        )
        alert2 = Alert(
            id="a2", rule_name="r2", metric="y", value=1.0, threshold=0.0,
            operator="gt", severity=Severity.WARNING, state=AlertState.FIRING,
            instance="inst2", description="",
            fired_at=(now - timedelta(hours=1)).replace(microsecond=0).isoformat(),
        )
        store.insert_alert(alert1)
        store.insert_alert(alert2)
        # Filter by rule_name
        assert len(store.query_alerts(rule_name="r1")) == 1
        # Filter by instance
        assert len(store.query_alerts(instance="inst2")) == 1
        # Filter by state
        assert len(store.query_alerts(state=AlertState.FIRING)) == 2
        # Filter by since_hours (only alerts within the last N hours)
        assert len(store.query_alerts(since_hours=0.5)) == 0   # both are > 30 min ago
        assert len(store.query_alerts(since_hours=24.0)) == 2  # both are within 24 h

    def test_count_firing_by_severity(self, tmp_path: Path) -> None:
        store = AlertStore(tmp_path / "alerts.sqlite")
        store.init_schema()
        for i in range(3):
            store.insert_alert(Alert(
                id=f"c{i}", rule_name="r", metric="x", value=1.0,
                threshold=0.0, operator="gt", severity=Severity.CRITICAL,
                state=AlertState.FIRING, instance="inst",
                description="", fired_at="2026-04-30T03:00:00+00:00",
            ))
        for i in range(2):
            store.insert_alert(Alert(
                id=f"w{i}", rule_name="r", metric="x", value=1.0,
                threshold=0.0, operator="gt", severity=Severity.WARNING,
                state=AlertState.FIRING, instance="inst",
                description="", fired_at="2026-04-30T03:00:00+00:00",
            ))
        counts = store.count_firing_by_severity()
        assert counts["critical"] == 3
        assert counts["warning"] == 2
        assert counts.get("info", 0) == 0

    def test_rules_crud(self, tmp_path: Path) -> None:
        store = AlertStore(tmp_path / "alerts.sqlite")
        store.init_schema()
        rule = AlertRule(
            name="test_rule",
            metric="cpu.usage",
            operator="gt",
            threshold=80.0,
            severity=Severity.CRITICAL,
            description="Test rule",
            labels={"team": "ops"},
        )
        store.upsert_rule(rule)
        rules = store.query_rules()
        assert len(rules) == 1
        assert rules[0].name == "test_rule"
        assert rules[0].labels["team"] == "ops"
        # Update
        rule2 = AlertRule(
            name="test_rule",
            metric="cpu.usage",
            operator="gt",
            threshold=90.0,
            severity=Severity.CRITICAL,
            description="Updated",
        )
        store.upsert_rule(rule2)
        rules = store.query_rules()
        assert rules[0].threshold == 90.0
        # Delete
        store.delete_rule("test_rule")
        assert len(store.query_rules()) == 0

    def test_insert_event(self, tmp_path: Path) -> None:
        store = AlertStore(tmp_path / "alerts.sqlite")
        store.init_schema()
        alert = Alert(
            id="ev-alert", rule_name="r", metric="x", value=1.0,
            threshold=0.0, operator="gt", severity=Severity.INFO,
            state=AlertState.FIRING, instance="inst",
            description="", fired_at="2026-04-30T03:00:00+00:00",
        )
        store.insert_alert(alert)
        event = AlertEvent(type="firing", alert=alert)
        store.insert_event(event)
        events = store.query_events()
        assert len(events) == 1
        assert events[0]["event_type"] == "firing"

    def test_delete_resolved_older_than(self, tmp_path: Path) -> None:
        store = AlertStore(tmp_path / "alerts.sqlite")
        store.init_schema()
        # Use timestamps relative to now so the test is not date-sensitive.
        now = datetime.now(tz=timezone.utc)
        old_resolved = (now - timedelta(days=30)).replace(microsecond=0).isoformat()
        recent_resolved = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()
        # Insert old resolved alert (definitely older than 24 h)
        store.insert_alert(Alert(
            id="old1", rule_name="r", metric="x", value=1.0,
            threshold=0.0, operator="gt", severity=Severity.INFO,
            state=AlertState.RESOLVED, instance="inst",
            description="", fired_at=old_resolved,
            resolved_at=old_resolved,
        ))
        # Insert recent resolved alert (well within 24 h — must NOT be deleted)
        store.insert_alert(Alert(
            id="new1", rule_name="r", metric="x", value=1.0,
            threshold=0.0, operator="gt", severity=Severity.INFO,
            state=AlertState.RESOLVED, instance="inst",
            description="", fired_at=recent_resolved,
            resolved_at=recent_resolved,
        ))
        deleted = store.delete_resolved_older_than(older_than_hours=24.0)
        assert deleted == 1
        remaining = store.query_alerts()
        assert len(remaining) == 1
        assert remaining[0].id == "new1"


# ---------------------------------------------------------------------------
# LogNotifier
# ---------------------------------------------------------------------------

class TestLogNotifier:
    def test_send_does_not_raise(self, caplog) -> None:
        # We just verify it doesn't raise
        notifier = LogNotifier()
        rule = AlertRule(
            name="test", metric="x", operator="gt", threshold=0.0,
            severity=Severity.WARNING, description="Test alert",
        )
        engine = AlertEngine(rules=[rule])
        events = engine.evaluate("x", 1.0, instance="inst")
        assert len(events) == 1
        notifier.send(events[0])  # Should not raise
        notifier.send_batch(events)  # Should not raise


# ---------------------------------------------------------------------------
# CompositeNotifier
# ---------------------------------------------------------------------------

class TestCompositeNotifier:
    def test_delegates_to_all_notifiers(self) -> None:
        received: list[str] = []

        class FakeNotifier(AlertNotifier):
            def send(self, event: AlertEvent) -> None:
                received.append(event.alert.rule_name)

            def send_batch(self, events: list[AlertEvent]) -> None:
                for e in events:
                    self.send(e)

        n1 = FakeNotifier()
        n2 = FakeNotifier()
        composite = CompositeNotifier([n1, n2])
        rule = AlertRule(
            name="test_rule", metric="x", operator="gt", threshold=0.0,
            severity=Severity.INFO, description="",
        )
        engine = AlertEngine(rules=[rule])
        events = engine.evaluate("x", 1.0, instance="inst")
        composite.send(events[0])
        # Both n1 and n2 should have received it
        assert len(received) == 2

    def test_add_remove_notifier(self) -> None:
        composite = CompositeNotifier()
        class FakeNotifier(AlertNotifier):
            def send(self, event: AlertEvent) -> None: pass
            def send_batch(self, events: list[AlertEvent]) -> None: pass

        n = FakeNotifier()
        composite.add(n)
        # Should not raise
        composite.remove(n)


# ---------------------------------------------------------------------------
# AlertPrometheusExporter
# ---------------------------------------------------------------------------

class TestAlertPrometheusExporter:
    def test_metrics_text_format(self) -> None:
        exporter = AlertPrometheusExporter(prefix="test_alert")
        alert = Alert(
            id="p1",
            rule_name="high_cpu",
            metric="cpu.usage",
            value=95.0,
            threshold=80.0,
            operator="gt",
            severity=Severity.CRITICAL,
            state=AlertState.FIRING,
            instance="pg-main-01",
            description="CPU high",
            fired_at="2026-04-30T03:00:00+00:00",
        )
        exporter.sync_alerts([alert])
        metrics = exporter.metrics_text
        assert "test_alert_firing" in metrics
        assert "test_alert_severity_level" in metrics
        assert 'alert_name="high_cpu"' in metrics
        assert 'severity="critical"' in metrics
        assert 'instance="pg-main-01"' in metrics
        assert "# HELP test_alert_firing" in metrics
        assert "# TYPE test_alert_firing gauge" in metrics

    def test_sync_summary(self) -> None:
        exporter = AlertPrometheusExporter(prefix="test_alert")
        exporter.sync_summary(firing=3, warning=2, critical=1, info=0)
        metrics = exporter.metrics_text
        assert "test_alert_total" in metrics
        # Check label values appear
        assert 'severity="firing"' in metrics

    def test_metrics_text_has_correct_values(self) -> None:
        exporter = AlertPrometheusExporter(prefix="test_alert")
        alert_firing = Alert(
            id="f1",
            rule_name="test",
            metric="x",
            value=1.0,
            threshold=0.0,
            operator="gt",
            severity=Severity.CRITICAL,
            state=AlertState.FIRING,
            instance="inst",
            description="",
            fired_at="2026-04-30T03:00:00+00:00",
        )
        alert_resolved = Alert(
            id="r1",
            rule_name="test2",
            metric="y",
            value=0.0,
            threshold=0.0,
            operator="gt",
            severity=Severity.INFO,
            state=AlertState.RESOLVED,
            instance="inst",
            description="",
            fired_at="2026-04-30T03:00:00+00:00",
            resolved_at="2026-04-30T04:00:00+00:00",
        )
        exporter.sync_alerts([alert_firing, alert_resolved])
        metrics = exporter.metrics_text
        # Firing alert should have value 1, resolved should have 0
        assert 'test_alert_firing{alert_name="test"' in metrics
        assert 'test_alert_firing{alert_name="test2"' in metrics
        # Verify the firing state line ends with "1.0" and resolved with "0.0"
        all_lines = metrics.splitlines()
        test1_line = next((l for l in all_lines if 'alert_name="test"' in l and 'state="firing"' in l), None)
        assert test1_line is not None
        assert test1_line.strip().endswith("1.0")
        test2_line = next((l for l in all_lines if 'alert_name="test2"' in l and 'state="resolved"' in l), None)
        assert test2_line is not None
        assert test2_line.strip().endswith("0.0")

    def test_start_stop_server(self) -> None:
        import threading
        port = 20000 + threading.current_thread().ident % 10000
        exporter = AlertPrometheusExporter(listen_port=port)
        exporter.start()
        try:
            assert exporter._thread is not None
            assert exporter._thread.is_alive()
        finally:
            exporter.stop()
        # After stop, thread should be gone
        assert exporter._thread is None


# ---------------------------------------------------------------------------
# Severity ordering
# ---------------------------------------------------------------------------

class TestSeverity:
    def test_severity_int_ordering(self) -> None:
        assert int(Severity.INFO) < int(Severity.WARNING)
        assert int(Severity.WARNING) < int(Severity.CRITICAL)
