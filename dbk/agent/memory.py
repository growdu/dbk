"""Agent memory system: episodic (conversation turns) and semantic (key facts) memory."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _utc_ts() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


# ----------------------------------------------------------------------
# Memory entry types.
# ----------------------------------------------------------------------


@dataclass
class Fact:
    """A semantic memory entry: a key-value fact observed during a session."""
    id: str
    session_id: str
    key: str
    value: str
    importance: int  # 1-10, higher = more important to retain
    created_at: str = field(default_factory=_utc_now)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "key": self.key,
            "value": self.value,
            "importance": self.importance,
            "created_at": self.created_at,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fact:
        return cls(
            id=data["id"],
            session_id=data["session_id"],
            key=data["key"],
            value=data["value"],
            importance=data.get("importance", 5),
            created_at=data.get("created_at", _utc_now()),
            tags=data.get("tags", []),
        )


@dataclass
class Summary:
    """A summarization of a recent window of conversation turns."""
    id: str
    session_id: str
    summary: str
    window_start: int  # turn_count at window start
    window_end: int  # turn_count at window end
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "summary": self.summary,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "created_at": self.created_at,
        }


# ----------------------------------------------------------------------
# Memory backends (pluggable).
# ----------------------------------------------------------------------


class MemoryBackend(ABC):
    """Abstract base for memory storage backends."""

    @abstractmethod
    def store_fact(self, fact: Fact) -> None:
        """Persist a fact entry."""
        raise NotImplementedError

    @abstractmethod
    def recall_facts(
        self,
        session_id: str | None = None,
        key_prefix: str | None = None,
        min_importance: int = 0,
        limit: int = 50,
    ) -> list[Fact]:
        """Retrieve facts, optionally filtered."""
        raise NotImplementedError

    @abstractmethod
    def forget_fact(self, fact_id: str) -> bool:
        """Remove a fact by id. Returns True if it existed."""
        raise NotImplementedError

    @abstractmethod
    def store_summary(self, summary: Summary) -> None:
        """Persist a conversation summary."""
        raise NotImplementedError

    @abstractmethod
    def recall_summaries(
        self,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[Summary]:
        """Retrieve recent summaries for a session."""
        raise NotImplementedError

    @abstractmethod
    def archive_turn(
        self,
        session_id: str,
        turn_count: int,
        role: str,
        content: str,
        metadata_json: str = "{}",
    ) -> None:
        """Archive a raw conversation turn to episodic memory."""
        raise NotImplementedError

    @abstractmethod
    def recall_episodes(
        self,
        session_id: str,
        since_turn: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Recall recent episodic memory entries for a session."""
        raise NotImplementedError

    @abstractmethod
    def get_context_for_prompt(
        self,
        session_id: str,
        max_facts: int = 10,
        max_episodes: int = 5,
    ) -> str:
        """Build a memory context string for injection into the system prompt."""
        raise NotImplementedError

    @abstractmethod
    def prune_session(self, session_id: str, retain_turns: int = 10) -> int:
        """Prune old episodic entries, retaining the most recent N turns.
        Returns the number of entries deleted."""
        raise NotImplementedError


# ----------------------------------------------------------------------
# SQLite-backed memory implementation.
# ----------------------------------------------------------------------


class SQLiteMemoryBackend(MemoryBackend):
    """SQLite-backed agent memory: facts, summaries, and episodic turns."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            from dbk.config import dbk_root
            db_path = dbk_root() / "agent_memory.sqlite"
        self._db_path = Path(db_path)
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_facts (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value TEXT NOT NULL,
                        importance INTEGER NOT NULL DEFAULT 5,
                        created_at TEXT NOT NULL,
                        tags_json TEXT NOT NULL DEFAULT '[]'
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_facts_session
                    ON memory_facts(session_id, created_at DESC)
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_summaries (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        window_start INTEGER NOT NULL,
                        window_end INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_summaries_session
                    ON memory_summaries(session_id, created_at DESC)
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_episodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        turn_count INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        archived_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_episodes_session_turn
                    ON memory_episodes(session_id, turn_count DESC)
                """)
                conn.commit()
            finally:
                conn.close()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path), check_same_thread=False)

    def store_fact(self, fact: Fact) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO memory_facts
                    (id, session_id, key, value, importance, created_at, tags_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact.id,
                        fact.session_id,
                        fact.key,
                        fact.value,
                        fact.importance,
                        fact.created_at,
                        json.dumps(fact.tags),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def recall_facts(
        self,
        session_id: str | None = None,
        key_prefix: str | None = None,
        min_importance: int = 0,
        limit: int = 50,
    ) -> list[Fact]:
        with self._lock:
            conn = self._conn()
            try:
                query = "SELECT * FROM memory_facts WHERE importance >= ?"
                params: list[Any] = [min_importance]
                if session_id:
                    query += " AND session_id = ?"
                    params.append(session_id)
                if key_prefix:
                    query += " AND key LIKE ?"
                    params.append(f"{key_prefix}%")
                query += " ORDER BY importance DESC, created_at DESC LIMIT ?"
                params.append(limit)
                cur = conn.execute(query, params)
                return [self._row_to_fact(cast(tuple[Any, ...], row), cast(tuple[tuple[str, ...], ...], cur.description)) for row in cur.fetchall()]
            finally:
                conn.close()

    def forget_fact(self, fact_id: str) -> bool:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute("DELETE FROM memory_facts WHERE id = ?", (fact_id,))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def store_summary(self, summary: Summary) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO memory_summaries
                    (id, session_id, summary, window_start, window_end, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary.id,
                        summary.session_id,
                        summary.summary,
                        summary.window_start,
                        summary.window_end,
                        summary.created_at,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def recall_summaries(
        self,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[Summary]:
        with self._lock:
            conn = self._conn()
            try:
                if session_id:
                    cur = conn.execute(
                        "SELECT * FROM memory_summaries WHERE session_id = ? "
                        "ORDER BY created_at DESC LIMIT ?",
                        (session_id, limit),
                    )
                else:
                    cur = conn.execute(
                        "SELECT * FROM memory_summaries ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    )
                return [self._row_to_summary(cast(tuple[Any, ...], row), cast(tuple[tuple[str, ...], ...], cur.description)) for row in cur.fetchall()]
            finally:
                conn.close()

    def archive_turn(
        self,
        session_id: str,
        turn_count: int,
        role: str,
        content: str,
        metadata_json: str = "{}",
    ) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    INSERT INTO memory_episodes
                    (session_id, turn_count, role, content, metadata_json, archived_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (session_id, turn_count, role, content, metadata_json, _utc_now()),
                )
                conn.commit()
            finally:
                conn.close()

    def recall_episodes(
        self,
        session_id: str,
        since_turn: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._conn()
            try:
                if since_turn is not None:
                    cur = conn.execute(
                        "SELECT * FROM memory_episodes WHERE session_id = ? AND turn_count > ? "
                        "ORDER BY turn_count DESC LIMIT ?",
                        (session_id, since_turn, limit),
                    )
                else:
                    cur = conn.execute(
                        "SELECT * FROM memory_episodes WHERE session_id = ? "
                        "ORDER BY turn_count DESC LIMIT ?",
                        (session_id, limit),
                    )
                return [self._row_to_episode(cast(tuple[Any, ...], row), cast(tuple[tuple[str, ...], ...], cur.description)) for row in cur.fetchall()]
            finally:
                conn.close()

    def get_context_for_prompt(
        self,
        session_id: str,
        max_facts: int = 10,
        max_episodes: int = 5,
    ) -> str:
        """Build a concise memory context string for injection into system prompt."""
        parts: list[str] = []

        # High-importance facts.
        facts = self.recall_facts(session_id=session_id, limit=max_facts)
        if facts:
            parts.append("[Key facts learned]")
            for f in facts[:max_facts]:
                parts.append(f"  - {f.key}: {f.value}")

        # Recent summaries.
        summaries = self.recall_summaries(session_id=session_id, limit=3)
        if summaries:
            parts.append("[Conversation summaries]")
            for s in summaries:
                parts.append(f"  [turns {s.window_start}-{s.window_end}]: {s.summary}")

        # Recent episodes.
        episodes = self.recall_episodes(session_id=session_id, limit=max_episodes)
        if episodes:
            parts.append("[Recent turns]")
            for ep in reversed(episodes[-max_episodes:]):
                role_tag = ep["role"][0].upper()
                content = ep["content"]
                if len(content) > 200:
                    content = content[:200] + "..."
                parts.append(f"  {role_tag}: {content}")

        return "\n".join(parts) if parts else ""

    def prune_session(self, session_id: str, retain_turns: int = 10) -> int:
        """Delete old episodic entries beyond the most recent N turns."""
        with self._lock:
            conn = self._conn()
            try:
                # Get the turn_count threshold.
                cur = conn.execute(
                    "SELECT turn_count FROM memory_episodes WHERE session_id = ? "
                    "ORDER BY turn_count DESC LIMIT 1 OFFSET ?",
                    (session_id, retain_turns),
                )
                row = cur.fetchone()
                if row is None:
                    return 0
                threshold = row[0]
                cur2 = conn.execute(
                    "DELETE FROM memory_episodes WHERE session_id = ? AND turn_count <= ?",
                    (session_id, threshold),
                )
                conn.commit()
                return cur2.rowcount
            finally:
                conn.close()

    def _row_to_fact(self, row: tuple[Any, ...], description: tuple[tuple[str, ...], ...]) -> Fact:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return Fact(
            id=data["id"],
            session_id=data["session_id"],
            key=data["key"],
            value=data["value"],
            importance=data.get("importance", 5),
            created_at=data.get("created_at", _utc_now()),
            tags=json.loads(data.get("tags_json", "[]")),
        )

    def _row_to_summary(self, row: tuple[Any, ...], description: tuple[tuple[str, ...], ...]) -> Summary:
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))
        return Summary(
            id=data["id"],
            session_id=data["session_id"],
            summary=data["summary"],
            window_start=data["window_start"],
            window_end=data["window_end"],
            created_at=data.get("created_at", _utc_now()),
        )

    def _row_to_episode(self, row: tuple[Any, ...], description: tuple[tuple[str, ...], ...]) -> dict[str, Any]:
        cols = [d[0] for d in description]
        return dict(zip(cols, row))


# ----------------------------------------------------------------------
# In-memory backend (for testing / single-process use).
# ----------------------------------------------------------------------


class InMemoryBackend(MemoryBackend):
    """Simple in-memory backend (no persistence)."""

    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}
        self._summaries: dict[str, Summary] = {}
        self._episodes: list[dict[str, Any]] = []
        self._ep_id_counter = 0

    def store_fact(self, fact: Fact) -> None:
        self._facts[fact.id] = fact

    def recall_facts(
        self,
        session_id: str | None = None,
        key_prefix: str | None = None,
        min_importance: int = 0,
        limit: int = 50,
    ) -> list[Fact]:
        facts = list(self._facts.values())
        if session_id:
            facts = [f for f in facts if f.session_id == session_id]
        if key_prefix:
            facts = [f for f in facts if f.key.startswith(key_prefix)]
        facts = [f for f in facts if f.importance >= min_importance]
        facts.sort(key=lambda f: (-f.importance, f.created_at))
        return facts[:limit]

    def forget_fact(self, fact_id: str) -> bool:
        return self._facts.pop(fact_id, None) is not None

    def store_summary(self, summary: Summary) -> None:
        self._summaries[summary.id] = summary

    def recall_summaries(
        self,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[Summary]:
        summaries = list(self._summaries.values())
        if session_id:
            summaries = [s for s in summaries if s.session_id == session_id]
        summaries.sort(key=lambda s: s.created_at, reverse=True)
        return summaries[:limit]

    def archive_turn(
        self,
        session_id: str,
        turn_count: int,
        role: str,
        content: str,
        metadata_json: str = "{}",
    ) -> None:
        self._ep_id_counter += 1
        self._episodes.append({
            "id": self._ep_id_counter,
            "session_id": session_id,
            "turn_count": turn_count,
            "role": role,
            "content": content,
            "metadata_json": metadata_json,
            "archived_at": _utc_now(),
        })

    def recall_episodes(
        self,
        session_id: str,
        since_turn: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        episodes = [e for e in self._episodes if e["session_id"] == session_id]
        if since_turn is not None:
            episodes = [e for e in episodes if e["turn_count"] > since_turn]
        episodes.sort(key=lambda e: e["turn_count"], reverse=True)
        return episodes[:limit]

    def get_context_for_prompt(
        self,
        session_id: str,
        max_facts: int = 10,
        max_episodes: int = 5,
    ) -> str:
        parts: list[str] = []
        facts = self.recall_facts(session_id=session_id, limit=max_facts)
        if facts:
            parts.append("[Key facts learned]")
            for f in facts[:max_facts]:
                parts.append(f"  - {f.key}: {f.value}")
        episodes = self.recall_episodes(session_id=session_id, limit=max_episodes)
        if episodes:
            parts.append("[Recent turns]")
            for ep in reversed(episodes[-max_episodes:]):
                parts.append(f"  {ep['role'][0].upper()}: {ep['content'][:200]}")
        return "\n".join(parts) if parts else ""

    def prune_session(self, session_id: str, retain_turns: int = 10) -> int:
        sessions_episodes = [e for e in self._episodes if e["session_id"] == session_id]
        sessions_episodes.sort(key=lambda e: e["turn_count"], reverse=True)
        threshold = sessions_episodes[retain_turns]["turn_count"] if len(sessions_episodes) > retain_turns else -1
        before = len(self._episodes)
        self._episodes = [e for e in self._episodes if e["session_id"] != session_id or e["turn_count"] > threshold]
        return before - len(self._episodes)


# ----------------------------------------------------------------------
# High-level AgentMemory facade.
# ----------------------------------------------------------------------


class AgentMemory:
    """High-level agent memory facade wrapping a MemoryBackend.

    Provides simple store/recall APIs with automatic fact importance scoring.
    """

    def __init__(
        self,
        backend: MemoryBackend | None = None,
    ) -> None:
        self._backend = backend or InMemoryBackend()

    # --- Facts ---

    def remember(
        self,
        session_id: str,
        key: str,
        value: str,
        importance: int = 5,
        tags: list[str] | None = None,
    ) -> Fact:
        """Store an important fact about a session."""
        fact = Fact(
            id=str(uuid.uuid4()),
            session_id=session_id,
            key=key,
            value=value,
            importance=min(10, max(1, importance)),
            tags=tags or [],
        )
        self._backend.store_fact(fact)
        return fact

    def recall(
        self,
        session_id: str | None = None,
        key_prefix: str | None = None,
        min_importance: int = 0,
        limit: int = 50,
    ) -> list[Fact]:
        """Recall facts matching the given filters."""
        return self._backend.recall_facts(
            session_id=session_id,
            key_prefix=key_prefix,
            min_importance=min_importance,
            limit=limit,
        )

    def forget(self, fact_id: str) -> bool:
        """Remove a fact by id."""
        return self._backend.forget_fact(fact_id)

    # --- Summaries ---

    def summarize(
        self,
        session_id: str,
        summary: str,
        window_start: int,
        window_end: int,
    ) -> Summary:
        """Record a conversation window summary."""
        s = Summary(
            id=str(uuid.uuid4()),
            session_id=session_id,
            summary=summary,
            window_start=window_start,
            window_end=window_end,
        )
        self._backend.store_summary(s)
        return s

    def get_summaries(
        self,
        session_id: str | None = None,
        limit: int = 5,
    ) -> list[Summary]:
        """Get recent summaries."""
        return self._backend.recall_summaries(session_id=session_id, limit=limit)

    # --- Episodic ---

    def archive_turn(
        self,
        session_id: str,
        turn_count: int,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Archive a conversation turn to episodic memory."""
        self._backend.archive_turn(
            session_id=session_id,
            turn_count=turn_count,
            role=role,
            content=content,
            metadata_json=json.dumps(metadata or {}),
        )

    def recall_episodes(
        self,
        session_id: str,
        since_turn: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Recall episodic memory entries."""
        return self._backend.recall_episodes(
            session_id=session_id,
            since_turn=since_turn,
            limit=limit,
        )

    def build_context(
        self,
        session_id: str,
        max_facts: int = 10,
        max_episodes: int = 5,
    ) -> str:
        """Build a memory context string for the system prompt."""
        return self._backend.get_context_for_prompt(
            session_id=session_id,
            max_facts=max_facts,
            max_episodes=max_episodes,
        )

    def prune(self, session_id: str, retain_turns: int = 10) -> int:
        """Prune old episodic entries, retaining N most recent turns."""
        return self._backend.prune_session(session_id, retain_turns)

    @property
    def backend(self) -> MemoryBackend:
        return self._backend
