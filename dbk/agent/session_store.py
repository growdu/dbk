"""SQLite-backed session persistence store."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from dbk.agent.state import AgentState, WorkflowStage


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class SessionStore:
    """SQLite-backed session persistence."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            from dbk.config import dbk_root
            db_path = dbk_root() / "agent_sessions.sqlite"
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS agent_sessions (
                        session_id TEXT PRIMARY KEY,
                        workflow_stage TEXT NOT NULL DEFAULT 'requirements',
                        workflow_goal TEXT NOT NULL DEFAULT '',
                        intent TEXT NOT NULL DEFAULT 'general',
                        tool_calls_json TEXT NOT NULL DEFAULT '[]',
                        last_tool_result_json TEXT,
                        conversation_history_json TEXT NOT NULL DEFAULT '[]',
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        turn_count INTEGER NOT NULL DEFAULT 0
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_sessions_updated
                    ON agent_sessions(updated_at DESC)
                """)
                conn.commit()
            finally:
                conn.close()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path), check_same_thread=False)

    def save(self, state: AgentState) -> None:
        """Persist a session state."""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO agent_sessions
                    (session_id, workflow_stage, workflow_goal, intent,
                     tool_calls_json, last_tool_result_json,
                     conversation_history_json, metadata_json,
                     created_at, updated_at, turn_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.session_id,
                        state.workflow_stage.value,
                        state.workflow_goal,
                        state.intent,
                        json.dumps(state.tool_calls),
                        json.dumps(state.last_tool_result) if state.last_tool_result else None,
                        json.dumps(state.conversation_history),
                        json.dumps(state.metadata),
                        state.created_at,
                        _utc_now(),
                        state.turn_count,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def load(self, session_id: str) -> AgentState | None:
        """Load a session by ID. Returns None if not found."""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "SELECT * FROM agent_sessions WHERE session_id = ?",
                    (session_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return self._row_to_state(cast(tuple[Any, ...], row), cast(tuple[tuple[str, ...], ...], cur.description))
            finally:
                conn.close()

    def delete(self, session_id: str) -> bool:
        """Delete a session. Returns True if it existed."""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_sessions WHERE session_id = ?",
                    (session_id,),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def list_sessions(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List recent sessions."""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    """
                    SELECT session_id, workflow_stage, workflow_goal, intent,
                           created_at, updated_at, turn_count
                    FROM agent_sessions
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                )
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, row)) for row in rows]
            finally:
                conn.close()

    def delete_older_than(self, older_than_iso: str) -> int:
        """Delete sessions older than a given ISO timestamp."""
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_sessions WHERE updated_at < ?",
                    (older_than_iso,),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    def _row_to_state(self, row: tuple[Any, ...], description: tuple[tuple[str, ...], ...]) -> AgentState:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return AgentState(
            session_id=data["session_id"],
            workflow_stage=WorkflowStage(data["workflow_stage"]),
            workflow_goal=data.get("workflow_goal", ""),
            intent=data.get("intent", "general"),
            tool_calls=json.loads(data.get("tool_calls_json", "[]")),
            last_tool_result=json.loads(data["last_tool_result_json"])
            if data.get("last_tool_result_json")
            else None,
            conversation_history=json.loads(data.get("conversation_history_json", "[]")),
            metadata=json.loads(data.get("metadata_json", "{}")),
            created_at=data.get("created_at", _utc_now()),
            updated_at=data.get("updated_at", _utc_now()),
            turn_count=data.get("turn_count", 0),
        )
