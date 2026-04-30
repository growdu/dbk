"""SQLite-backed alert store for persisting alert history."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dbk.alerting.models import Alert, AlertEvent, AlertRule, AlertState, Severity


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class AlertStore:
    """Persistent SQLite store for alerts and alert rules."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert (
                    id TEXT PRIMARY KEY,
                    rule_name TEXT NOT NULL,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    threshold REAL NOT NULL,
                    operator TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    state TEXT NOT NULL,
                    instance TEXT NOT NULL,
                    description TEXT,
                    fired_at TEXT NOT NULL,
                    resolved_at TEXT,
                    acknowledged_at TEXT,
                    labels_json TEXT,
                    annotations_json TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alert_rule_name ON alert(rule_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alert_state ON alert(state)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alert_fired_at ON alert(fired_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_rule (
                    name TEXT PRIMARY KEY,
                    metric TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    threshold REAL NOT NULL,
                    severity TEXT NOT NULL,
                    description TEXT,
                    instance TEXT,
                    minimum_duration_sec INTEGER DEFAULT 0,
                    cooldown_sec INTEGER DEFAULT 300,
                    labels_json TEXT,
                    enabled INTEGER DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    metadata_json TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_alert_id ON alert_event(alert_id)"
            )

    # ------------------------------------------------------------------
    # Alert CRUD
    # ------------------------------------------------------------------

    def insert_alert(self, alert: Alert) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO alert
                (id, rule_name, metric, value, threshold, operator, severity, state,
                 instance, description, fired_at, resolved_at, acknowledged_at,
                 labels_json, annotations_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.id,
                    alert.rule_name,
                    alert.metric,
                    alert.value,
                    alert.threshold,
                    alert.operator,
                    alert.severity.value,
                    alert.state.value,
                    alert.instance,
                    alert.description,
                    alert.fired_at,
                    alert.resolved_at,
                    alert.acknowledged_at,
                    json.dumps(alert.labels, ensure_ascii=True),
                    json.dumps(alert.annotations, ensure_ascii=True),
                ),
            )

    def update_alert_state(self, alert_id: str, state: AlertState, resolved_at: str | None = None) -> None:
        resolved_col = "resolved_at = ?" if resolved_at else "resolved_at = NULL"
        params: list[object] = [state.value]
        if resolved_at:
            params.append(resolved_at)
        params.append(alert_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE alert SET state = ?, {resolved_col} WHERE id = ?",
                params,
            )

    def query_firing_alerts(self, limit: int = 100) -> list[Alert]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM alert WHERE state = ? ORDER BY fired_at DESC LIMIT ?
                """,
                (AlertState.FIRING.value, limit),
            )
            return [self._row_to_alert(row) for row in rows]

    def query_alerts(
        self,
        *,
        rule_name: str | None = None,
        instance: str | None = None,
        state: AlertState | None = None,
        since_hours: float | None = None,
        limit: int = 100,
    ) -> list[Alert]:
        sql = "SELECT * FROM alert WHERE 1=1"
        params: list[object] = []
        if rule_name:
            sql += " AND rule_name = ?"
            params.append(rule_name)
        if instance:
            sql += " AND instance = ?"
            params.append(instance)
        if state:
            sql += " AND state = ?"
            params.append(state.value)
        if since_hours:
            cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=since_hours)
            sql += " AND fired_at >= ?"
            params.append(cutoff.isoformat())
        sql += " ORDER BY fired_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params))
            return [self._row_to_alert(row) for row in rows]

    def count_firing_by_severity(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT severity, COUNT(*) FROM alert WHERE state = ? GROUP BY severity",
                (AlertState.FIRING.value,),
            )
            return {str(r[0]): int(r[1]) for r in rows}

    # ------------------------------------------------------------------
    # Alert event logging
    # ------------------------------------------------------------------

    def insert_event(self, event: AlertEvent) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_event (alert_id, event_type, fired_at, metadata_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event.alert.id,
                    event.type,
                    event.fired_at,
                    json.dumps(event.alert.to_dict(), ensure_ascii=True),
                ),
            )

    def query_events(
        self,
        *,
        alert_id: str | None = None,
        event_type: str | None = None,
        since_hours: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alert_event WHERE 1=1"
        params: list[object] = []
        if alert_id:
            sql += " AND alert_id = ?"
            params.append(alert_id)
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        if since_hours:
            cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=since_hours)
            sql += " AND fired_at >= ?"
            params.append(cutoff.isoformat())
        sql += " ORDER BY fired_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params))
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Alert rules
    # ------------------------------------------------------------------

    def upsert_rule(self, rule: AlertRule) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO alert_rule
                (name, metric, operator, threshold, severity, description,
                 instance, minimum_duration_sec, cooldown_sec, labels_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.name,
                    rule.metric,
                    rule.operator,
                    rule.threshold,
                    rule.severity.value,
                    rule.description,
                    rule.instance,
                    rule.minimum_duration_sec,
                    rule.cooldown_sec,
                    json.dumps(rule.labels, ensure_ascii=True),
                ),
            )

    def delete_rule(self, name: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM alert_rule WHERE name = ?", (name,))

    def query_rules(self, enabled: bool | None = None) -> list[AlertRule]:
        sql = "SELECT * FROM alert_rule"
        params: list[object] = []
        if enabled is not None:
            sql += " WHERE enabled = ?"
            params.append(1 if enabled else 0)
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params))
            return [self._row_to_rule(row) for row in rows]

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def delete_resolved_older_than(self, *, older_than_hours: float) -> int:
        if older_than_hours <= 0:
            raise ValueError("older_than_hours must be > 0")
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=older_than_hours)
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM alert WHERE state = ? AND resolved_at < ?",
                (AlertState.RESOLVED.value, cutoff.isoformat()),
            )
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_alert(row: sqlite3.Row) -> Alert:
        labels_raw = row["labels_json"]
        annotations_raw = row["annotations_json"]
        return Alert(
            id=str(row["id"]),
            rule_name=str(row["rule_name"]),
            metric=str(row["metric"]),
            value=float(row["value"]),
            threshold=float(row["threshold"]),
            operator=str(row["operator"]),
            severity=Severity(row["severity"]),
            state=AlertState(row["state"]),
            instance=str(row["instance"]),
            description=str(row["description"]) if row["description"] else "",
            fired_at=str(row["fired_at"]),
            resolved_at=str(row["resolved_at"]) if row["resolved_at"] else None,
            acknowledged_at=str(row["acknowledged_at"]) if row["acknowledged_at"] else None,
            labels=json.loads(labels_raw) if labels_raw else {},
            annotations=json.loads(annotations_raw) if annotations_raw else {},
        )

    @staticmethod
    def _row_to_rule(row: sqlite3.Row) -> AlertRule:
        labels_raw = row["labels_json"]
        return AlertRule(
            name=str(row["name"]),
            metric=str(row["metric"]),
            operator=str(row["operator"]),
            threshold=float(row["threshold"]),
            severity=Severity(row["severity"]),
            description=str(row["description"]) if row["description"] else "",
            instance=str(row["instance"]) if row["instance"] else None,
            minimum_duration_sec=int(row["minimum_duration_sec"]),
            cooldown_sec=int(row["cooldown_sec"]),
            labels=json.loads(labels_raw) if labels_raw else {},
        )
