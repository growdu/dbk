from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import RuntimeEvent, TraceArtifact


class RuntimeStore:
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
                CREATE TABLE IF NOT EXISTS runtime_metric (
                  id INTEGER PRIMARY KEY,
                  ts TEXT NOT NULL,
                  instance TEXT NOT NULL,
                  source TEXT NOT NULL,
                  category TEXT NOT NULL,
                  metric TEXT NOT NULL,
                  value REAL NOT NULL,
                  labels_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runtime_metric_metric_ts
                ON runtime_metric(metric, ts DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_artifact (
                  id INTEGER PRIMARY KEY,
                  task_id TEXT NOT NULL,
                  profile TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  duration_sec INTEGER NOT NULL,
                  artifact_path TEXT NOT NULL,
                  summary_json TEXT
                )
                """
            )

    def insert_events(self, events: list[RuntimeEvent]) -> int:
        if not events:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO runtime_metric
                (ts, instance, source, category, metric, value, labels_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        event.ts,
                        event.instance,
                        event.source,
                        event.category,
                        event.metric,
                        event.value,
                        json.dumps(event.labels, ensure_ascii=True),
                    )
                    for event in events
                ],
            )
        return len(events)

    def insert_trace_artifact(self, artifact: TraceArtifact) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO trace_artifact
                (task_id, profile, started_at, duration_sec, artifact_path, summary_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.task_id,
                    artifact.profile,
                    artifact.started_at,
                    artifact.duration_sec,
                    artifact.artifact_path,
                    json.dumps(artifact.summary_json, ensure_ascii=True),
                ),
            )

    def query_latest_metric(
        self,
        metric: str,
        instance: str | None = None,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT ts, instance, source, category, metric, value, labels_json
            FROM runtime_metric
            WHERE metric = ?
        """
        params: list[object] = [metric]
        if instance:
            sql += " AND instance = ?"
            params.append(instance)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return list(conn.execute(sql, tuple(params)))

    def query_latest_metrics_by_prefix(
        self,
        metric_prefix: str,
        instance: str | None = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT ts, instance, source, category, metric, value, labels_json
            FROM runtime_metric
            WHERE metric LIKE ?
        """
        params: list[object] = [f"{metric_prefix}%"]
        if instance:
            sql += " AND instance = ?"
            params.append(instance)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return list(conn.execute(sql, tuple(params)))

