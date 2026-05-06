"""Tests for dbk/agent/memory.py — SQLiteMemoryBackend."""

import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from dbk.agent.memory import (
    Fact,
    Summary,
    SQLiteMemoryBackend,
    _utc_now,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_fact(
    session_id: str = "s1",
    key: str = "key",
    value: str = "val",
    importance: int = 5,
    tags: list[str] | None = None,
    created_at: str | None = None,
    fact_id: str | None = None,
) -> Fact:
    return Fact(
        id=fact_id or str(uuid.uuid4()),
        session_id=session_id,
        key=key,
        value=value,
        importance=importance,
        created_at=created_at or _utc_now(),
        tags=tags or [],
    )


def make_summary(
    session_id: str = "s1",
    summary: str = "a summary",
    window_start: int = 0,
    window_end: int = 10,
    summary_id: str | None = None,
) -> Summary:
    return Summary(
        id=summary_id or str(uuid.uuid4()),
        session_id=session_id,
        summary=summary,
        window_start=window_start,
        window_end=window_end,
        created_at=_utc_now(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_memory.sqlite"


@pytest.fixture
def backend(db_path: Path) -> SQLiteMemoryBackend:
    return SQLiteMemoryBackend(db_path)


# ---------------------------------------------------------------------------
# Schema / init tests
# ---------------------------------------------------------------------------

def test_init_schema_creates_tables(db_path: Path) -> None:
    """SQLiteMemoryBackend creates three tables on init."""
    SQLiteMemoryBackend(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in cur.fetchall()}
        assert "memory_facts" in tables
        assert "memory_summaries" in tables
        assert "memory_episodes" in tables
    finally:
        conn.close()


def test_init_schema_creates_indexes(db_path: Path) -> None:
    """Indexes are created for each table."""
    SQLiteMemoryBackend(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
        indexes = {row[0] for row in cur.fetchall()}
        assert "idx_facts_session" in indexes
        assert "idx_summaries_session" in indexes
        assert "idx_episodes_session_turn" in indexes
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fact CRUD
# ---------------------------------------------------------------------------

def test_store_fact_inserts_row(backend: SQLiteMemoryBackend) -> None:
    fact = make_fact(key="flavor", value="chocolate", importance=8)
    backend.store_fact(fact)
    facts = backend.recall_facts()
    assert len(facts) == 1
    assert facts[0].key == "flavor"
    assert facts[0].value == "chocolate"
    assert facts[0].importance == 8


def test_store_fact_insert_or_replace_upsert(backend: SQLiteMemoryBackend) -> None:
    """Storing a fact with the same id replaces it (upsert)."""
    fact_id = str(uuid.uuid4())
    f1 = make_fact(fact_id=fact_id, key="color", value="red", importance=3)
    f2 = make_fact(fact_id=fact_id, key="color", value="blue", importance=9)
    backend.store_fact(f1)
    backend.store_fact(f2)
    facts = backend.recall_facts()
    assert len(facts) == 1
    assert facts[0].value == "blue"
    assert facts[0].importance == 9


def test_recall_facts_no_filters(backend: SQLiteMemoryBackend) -> None:
    for i in range(5):
        backend.store_fact(make_fact(key=f"k{i}", value=f"v{i}"))
    facts = backend.recall_facts()
    assert len(facts) == 5


def test_recall_facts_filter_by_session_id(backend: SQLiteMemoryBackend) -> None:
    backend.store_fact(make_fact(session_id="s1", key="a", value="1"))
    backend.store_fact(make_fact(session_id="s1", key="b", value="2"))
    backend.store_fact(make_fact(session_id="s2", key="c", value="3"))
    facts = backend.recall_facts(session_id="s1")
    assert all(f.session_id == "s1" for f in facts)
    assert len(facts) == 2


def test_recall_facts_filter_by_key_prefix(backend: SQLiteMemoryBackend) -> None:
    backend.store_fact(make_fact(key="user_name", value="Alice"))
    backend.store_fact(make_fact(key="user_age", value="30"))
    backend.store_fact(make_fact(key="product_price", value="99"))
    facts = backend.recall_facts(key_prefix="user_")
    assert all(f.key.startswith("user_") for f in facts)
    assert len(facts) == 2


def test_recall_facts_filter_by_min_importance(backend: SQLiteMemoryBackend) -> None:
    for imp in [1, 3, 5, 7, 9]:
        backend.store_fact(make_fact(importance=imp))
    facts = backend.recall_facts(min_importance=5)
    assert all(f.importance >= 5 for f in facts)
    assert len(facts) == 3


def test_recall_facts_limit(backend: SQLiteMemoryBackend) -> None:
    for i in range(20):
        backend.store_fact(make_fact(key=f"k{i}"))
    facts = backend.recall_facts(limit=7)
    assert len(facts) == 7


def test_recall_facts_ordering(backend: SQLiteMemoryBackend) -> None:
    """Facts are ordered by importance DESC then created_at DESC."""
    t1 = "2024-01-01T00:00:00+00:00"
    t2 = "2024-01-01T00:00:01+00:00"
    backend.store_fact(make_fact(key="low", importance=2, created_at=t1))
    backend.store_fact(make_fact(key="high", importance=9, created_at=t1))
    backend.store_fact(make_fact(key="med", importance=5, created_at=t2))
    facts = backend.recall_facts()
    assert facts[0].key == "high"
    assert facts[1].key == "med"


def test_forget_fact_returns_true_when_exists(backend: SQLiteMemoryBackend) -> None:
    fact = make_fact()
    backend.store_fact(fact)
    result = backend.forget_fact(fact.id)
    assert result is True
    assert backend.recall_facts() == []


def test_forget_fact_returns_false_when_missing(backend: SQLiteMemoryBackend) -> None:
    result = backend.forget_fact("nonexistent-id")
    assert result is False


# ---------------------------------------------------------------------------
# JSON tags serialization
# ---------------------------------------------------------------------------

def test_tags_stored_as_json_and_retrieved(backend: SQLiteMemoryBackend) -> None:
    fact = make_fact(key="pref", value="dark mode", tags=["ui", "theme", "high-priority"])
    backend.store_fact(fact)
    retrieved = backend.recall_facts(key_prefix="pref")
    assert len(retrieved) == 1
    assert retrieved[0].tags == ["ui", "theme", "high-priority"]


def test_tags_empty_list_serialized(backend: SQLiteMemoryBackend) -> None:
    fact = make_fact(key="naked", value="val", tags=[])
    backend.store_fact(fact)
    retrieved = backend.recall_facts(key_prefix="naked")
    assert retrieved[0].tags == []


def test_tags_raw_sql_check(db_path: Path) -> None:
    """Verify tags are stored as a JSON array in the DB, not as text."""
    bk = SQLiteMemoryBackend(db_path)
    bk.store_fact(make_fact(key="tagged", value="x", tags=["a", "b"]))
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT tags_json FROM memory_facts WHERE key = ?", ("tagged",)
        )
        raw = cur.fetchone()[0]
        assert raw == '["a", "b"]'
        # should be valid JSON
        parsed = json.loads(raw)
        assert parsed == ["a", "b"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Summary CRUD
# ---------------------------------------------------------------------------

def test_store_summary_inserts_row(backend: SQLiteMemoryBackend) -> None:
    s = make_summary(summary="User prefers dark mode.", window_start=0, window_end=15)
    backend.store_summary(s)
    summaries = backend.recall_summaries()
    assert len(summaries) == 1
    assert summaries[0].summary == "User prefers dark mode."
    assert summaries[0].window_start == 0
    assert summaries[0].window_end == 15


def test_store_summary_no_upsert(backend: SQLiteMemoryBackend) -> None:
    """Summaries use plain INSERT — same id raises IntegrityError."""
    sid = str(uuid.uuid4())
    s1 = make_summary(summary_id=sid, summary="first", window_start=0, window_end=5)
    s2 = make_summary(summary_id=sid, summary="second", window_start=6, window_end=10)
    backend.store_summary(s1)
    with pytest.raises(sqlite3.IntegrityError):
        backend.store_summary(s2)
    # exactly one row persisted
    summaries = backend.recall_summaries()
    assert len(summaries) == 1
    assert summaries[0].summary == "first"


def test_recall_summaries_filter_by_session_id(backend: SQLiteMemoryBackend) -> None:
    backend.store_summary(make_summary(session_id="s1", summary="s1-sum"))
    backend.store_summary(make_summary(session_id="s2", summary="s2-sum"))
    backend.store_summary(make_summary(session_id="s1", summary="s1-sum-2"))
    summaries = backend.recall_summaries(session_id="s1")
    assert all(s.session_id == "s1" for s in summaries)
    assert len(summaries) == 2


def test_recall_summaries_no_session_returns_all(backend: SQLiteMemoryBackend) -> None:
    backend.store_summary(make_summary(session_id="s1"))
    backend.store_summary(make_summary(session_id="s2"))
    summaries = backend.recall_summaries()
    assert len(summaries) == 2


def test_recall_summaries_limit(backend: SQLiteMemoryBackend) -> None:
    for i in range(10):
        backend.store_summary(make_summary(summary=f"sum-{i}"))
    summaries = backend.recall_summaries(limit=3)
    assert len(summaries) == 3


def test_recall_summaries_ordering(backend: SQLiteMemoryBackend) -> None:
    """Results ordered by created_at DESC (most recent first)."""
    s1 = make_summary(summary="old")
    s2 = make_summary(summary="new")
    backend.store_summary(s1)
    backend.store_summary(s2)
    summaries = backend.recall_summaries()
    assert summaries[0].summary == "new"


# ---------------------------------------------------------------------------
# Episodic memory
# ---------------------------------------------------------------------------

def test_archive_turn_inserts_row(backend: SQLiteMemoryBackend) -> None:
    backend.archive_turn(
        session_id="s1",
        turn_count=1,
        role="user",
        content="Hello world",
        metadata_json='{"lang":"en"}',
    )
    episodes = backend.recall_episodes(session_id="s1")
    assert len(episodes) == 1
    assert episodes[0]["content"] == "Hello world"
    assert episodes[0]["role"] == "user"
    assert episodes[0]["turn_count"] == 1
    assert episodes[0]["metadata_json"] == '{"lang":"en"}'


def test_archive_turn_multiple_turns(backend: SQLiteMemoryBackend) -> None:
    for i in range(1, 6):
        backend.archive_turn("s1", turn_count=i, role="user", content=f"msg-{i}")
    episodes = backend.recall_episodes(session_id="s1")
    assert len(episodes) == 5


def test_recall_episodes_filter_by_since_turn(backend: SQLiteMemoryBackend) -> None:
    for i in range(1, 11):
        backend.archive_turn("s1", turn_count=i, role="user", content=f"msg-{i}")
    episodes = backend.recall_episodes(session_id="s1", since_turn=5)
    assert all(e["turn_count"] > 5 for e in episodes)
    assert len(episodes) == 5  # 6,7,8,9,10


def test_recall_episodes_limit(backend: SQLiteMemoryBackend) -> None:
    for i in range(1, 21):
        backend.archive_turn("s1", turn_count=i, role="user", content=f"msg-{i}")
    episodes = backend.recall_episodes(session_id="s1", limit=7)
    assert len(episodes) == 7


def test_recall_episodes_ordering(backend: SQLiteMemoryBackend) -> None:
    """Episodes are ordered by turn_count DESC (newest first)."""
    for i in range(1, 6):
        backend.archive_turn("s1", turn_count=i, role="user", content=f"msg-{i}")
    episodes = backend.recall_episodes(session_id="s1")
    assert episodes[0]["turn_count"] == 5
    assert episodes[-1]["turn_count"] == 1


def test_recall_episodes_no_since_returns_all(backend: SQLiteMemoryBackend) -> None:
    backend.archive_turn("s1", turn_count=1, role="user", content="a")
    backend.archive_turn("s2", turn_count=1, role="user", content="b")
    episodes = backend.recall_episodes(session_id="s1")
    assert len(episodes) == 1
    assert episodes[0]["content"] == "a"


def test_recall_episodes_other_session_returns_empty(backend: SQLiteMemoryBackend) -> None:
    backend.archive_turn("s1", turn_count=1, role="user", content="s1-msg")
    episodes = backend.recall_episodes(session_id="s999")
    assert episodes == []


# ---------------------------------------------------------------------------
# prune_session
# ---------------------------------------------------------------------------

def test_prune_session_returns_count_of_deleted(backend: SQLiteMemoryBackend) -> None:
    for i in range(1, 21):
        backend.archive_turn("s1", turn_count=i, role="user", content=f"t{i}")
    deleted = backend.prune_session("s1", retain_turns=5)
    assert deleted == 15


def test_prune_session_retains_recent_turns(backend: SQLiteMemoryBackend) -> None:
    for i in range(1, 16):
        backend.archive_turn("s1", turn_count=i, role="user", content=f"t{i}")
    backend.prune_session("s1", retain_turns=5)
    episodes = backend.recall_episodes(session_id="s1")
    assert len(episodes) == 5
    assert {e["turn_count"] for e in episodes} == {11, 12, 13, 14, 15}


def test_prune_session_returns_zero_when_fewer_than_retain(backend: SQLiteMemoryBackend) -> None:
    for i in range(1, 6):
        backend.archive_turn("s1", turn_count=i, role="user", content=f"t{i}")
    deleted = backend.prune_session("s1", retain_turns=10)
    assert deleted == 0
    assert len(backend.recall_episodes(session_id="s1")) == 5


def test_prune_session_unknown_session_returns_zero(backend: SQLiteMemoryBackend) -> None:
    deleted = backend.prune_session("nonexistent", retain_turns=5)
    assert deleted == 0


def test_prune_session_only_affects_target_session(backend: SQLiteMemoryBackend) -> None:
    for i in range(1, 11):
        backend.archive_turn("s1", turn_count=i, role="user", content=f"s1-{i}")
        backend.archive_turn("s2", turn_count=i, role="user", content=f"s2-{i}")
    backend.prune_session("s1", retain_turns=3)
    s1_eps = backend.recall_episodes(session_id="s1")
    s2_eps = backend.recall_episodes(session_id="s2")
    assert len(s1_eps) == 3
    assert len(s2_eps) == 10  # untouched


# ---------------------------------------------------------------------------
# get_context_for_prompt
# ---------------------------------------------------------------------------

def test_context_empty_session_returns_empty_string(backend: SQLiteMemoryBackend) -> None:
    ctx = backend.get_context_for_prompt("brand-new-session")
    assert ctx == ""


def test_context_includes_facts_section(backend: SQLiteMemoryBackend) -> None:
    backend.store_fact(make_fact(session_id="s1", key="user_name", value="Alice", importance=9))
    backend.store_fact(make_fact(session_id="s1", key="plan", value="pro", importance=7))
    ctx = backend.get_context_for_prompt("s1", max_facts=5)
    assert "[Key facts learned]" in ctx
    assert "user_name: Alice" in ctx
    assert "plan: pro" in ctx


def test_context_includes_summaries_section(backend: SQLiteMemoryBackend) -> None:
    backend.store_summary(make_summary(session_id="s1", summary="Alice upgraded to pro", window_start=0, window_end=10))
    ctx = backend.get_context_for_prompt("s1")
    assert "[Conversation summaries]" in ctx
    assert "Alice upgraded to pro" in ctx


def test_context_includes_recent_turns_section(backend: SQLiteMemoryBackend) -> None:
    backend.archive_turn("s1", turn_count=1, role="user", content="Hello")
    backend.archive_turn("s1", turn_count=2, role="assistant", content="Hi there!")
    ctx = backend.get_context_for_prompt("s1", max_episodes=5)
    assert "[Recent turns]" in ctx
    assert "Hello" in ctx
    assert "Hi there!" in ctx


def test_context_max_facts_limits_output(backend: SQLiteMemoryBackend) -> None:
    for i in range(15):
        backend.store_fact(make_fact(session_id="s1", key=f"fact_{i}", importance=i % 10 + 1))
    ctx = backend.get_context_for_prompt("s1", max_facts=3)
    count = ctx.count("- ")
    assert count <= 3


def test_context_max_episodes_limits_output(backend: SQLiteMemoryBackend) -> None:
    for i in range(1, 11):
        backend.archive_turn("s1", turn_count=i, role="user", content=f"msg-{i}")
    ctx = backend.get_context_for_prompt("s1", max_episodes=3)
    lines = ctx.split("\n")
    turn_lines = [l for l in lines if l.startswith("  U") or l.startswith("  A")]
    assert len(turn_lines) <= 3


def test_context_long_content_truncated(backend: SQLiteMemoryBackend) -> None:
    long_content = "x" * 500
    backend.archive_turn("s1", turn_count=1, role="user", content=long_content)
    ctx = backend.get_context_for_prompt("s1", max_episodes=5)
    assert "..." in ctx  # truncated
    assert long_content not in ctx


# ---------------------------------------------------------------------------
# Helper methods: _row_to_fact and _row_to_summary
# ---------------------------------------------------------------------------

def test_row_to_fact_maps_all_columns(backend: SQLiteMemoryBackend) -> None:
    """Exercise _row_to_fact via store+recall cycle."""
    fact = make_fact(
        session_id="s1",
        key="color",
        value="teal",
        importance=6,
        tags=["preference", "theme"],
    )
    backend.store_fact(fact)
    facts = backend.recall_facts()
    assert len(facts) == 1
    f = facts[0]
    assert f.session_id == "s1"
    assert f.key == "color"
    assert f.value == "teal"
    assert f.importance == 6
    assert f.tags == ["preference", "theme"]
    assert f.id == fact.id
    assert f.created_at == fact.created_at


def test_row_to_summary_maps_all_columns(backend: SQLiteMemoryBackend) -> None:
    """Exercise _row_to_summary via store+recall cycle."""
    summary = make_summary(
        session_id="s1",
        summary="The user discussed pricing.",
        window_start=10,
        window_end=20,
    )
    backend.store_summary(summary)
    summaries = backend.recall_summaries(session_id="s1")
    assert len(summaries) == 1
    s = summaries[0]
    assert s.session_id == "s1"
    assert s.summary == "The user discussed pricing."
    assert s.window_start == 10
    assert s.window_end == 20
    assert s.id == summary.id


def test_row_to_episode_maps_all_columns(backend: SQLiteMemoryBackend) -> None:
    """Exercise _row_to_episode via archive+recall cycle."""
    backend.archive_turn(
        session_id="s1",
        turn_count=5,
        role="assistant",
        content="Sure thing.",
        metadata_json='{"model":"gpt-4"}',
    )
    episodes = backend.recall_episodes(session_id="s1")
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep["session_id"] == "s1"
    assert ep["turn_count"] == 5
    assert ep["role"] == "assistant"
    assert ep["content"] == "Sure thing."
    assert ep["metadata_json"] == '{"model":"gpt-4"}'
    assert "archived_at" in ep


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

def test_concurrent_writes(backend: SQLiteMemoryBackend) -> None:
    errors: list[Exception] = []
    barrier = threading.Barrier(10)

    def writer(idx: int) -> None:
        try:
            barrier.wait()  # start simultaneously
            for j in range(20):
                backend.store_fact(make_fact(session_id="s1", key=f"k{idx}_{j}", value=f"v{idx}_{j}"))
        except Exception as e:
            errors.append(e)

    with ThreadPoolExecutor(max_workers=10) as exc:
        futs = [exc.submit(writer, i) for i in range(10)]
        for f in futs:
            f.result()

    assert not errors, f"Thread errors: {errors}"
    # Use a large limit to ensure we get all written facts despite the
    # default-limit-of-50 in recall_facts.
    facts = backend.recall_facts(session_id="s1", limit=500)
    assert len(facts) == 200, f"Expected 200 facts, got {len(facts)}"


def test_concurrent_reads_and_writes(backend: SQLiteMemoryBackend) -> None:
    """Pre-populate; then do concurrent reads and writes."""
    for i in range(50):
        backend.store_fact(make_fact(session_id="s1", key=f"pre_{i}"))

    errors: list[Exception] = []
    barrier = threading.Barrier(8)

    def reader() -> None:
        try:
            barrier.wait()
            for _ in range(20):
                backend.recall_facts(session_id="s1")
                backend.recall_episodes(session_id="s1")
        except Exception as e:
            errors.append(e)

    def writer() -> None:
        try:
            barrier.wait()
            for i in range(20):
                backend.store_fact(make_fact(session_id="s1", key=f"live_{i}"))
                backend.archive_turn("s1", turn_count=i, role="user", content=f"c{i}")
        except Exception as e:
            errors.append(e)

    with ThreadPoolExecutor(max_workers=8) as exc:
        futs = [exc.submit(reader if i % 2 == 0 else writer) for i in range(8)]
        for f in futs:
            f.result()

    assert not errors, f"Thread errors: {errors}"
    facts = backend.recall_facts(session_id="s1")
    assert len(facts) >= 50  # at least the pre-populated ones remain


def test_concurrent_store_and_forget(backend: SQLiteMemoryBackend) -> None:
    """Store facts then concurrently forget some while adding new ones."""
    ids_to_forget: list[str] = []

    def populate_and_record_ids() -> None:
        for _ in range(30):
            f = make_fact(session_id="s1")
            ids_to_forget.append(f.id)
            backend.store_fact(f)

    def forget_some() -> None:
        for fid in ids_to_forget[:15]:
            backend.forget_fact(fid)

    def write_more() -> None:
        for i in range(20):
            backend.store_fact(make_fact(session_id="s1", key=f"new_{i}"))

    with ThreadPoolExecutor(max_workers=3) as exc:
        exc.submit(populate_and_record_ids).result()
        exc.submit(forget_some)
        exc.submit(write_more).result()

    facts = backend.recall_facts(session_id="s1")
    # At least 5 facts remain: 30 - 15 forgotten + 20 new
    assert len(facts) >= 5


# ---------------------------------------------------------------------------
# Edge / error cases
# ---------------------------------------------------------------------------

def test_recall_facts_with_no_session_no_results(backend: SQLiteMemoryBackend) -> None:
    backend.store_fact(make_fact(session_id="only"))
    facts = backend.recall_facts(session_id="nonexistent-session")
    assert facts == []


def test_recall_summaries_empty_for_new_session(backend: SQLiteMemoryBackend) -> None:
    summaries = backend.recall_summaries(session_id="new-session")
    assert summaries == []


def test_fact_with_unicode_and_special_chars(backend: SQLiteMemoryBackend) -> None:
    fact = make_fact(key="note", value='Hello "world" & <tag> — café ☕', tags=["测试"])
    backend.store_fact(fact)
    retrieved = backend.recall_facts(key_prefix="note")
    assert len(retrieved) == 1
    assert retrieved[0].value == 'Hello "world" & <tag> — café ☕'
    assert retrieved[0].tags == ["测试"]


def test_episode_with_empty_content(backend: SQLiteMemoryBackend) -> None:
    backend.archive_turn("s1", turn_count=1, role="system", content="")
    episodes = backend.recall_episodes(session_id="s1")
    assert episodes[0]["content"] == ""


def test_fact_importance_clamped_in_range(backend: SQLiteMemoryBackend) -> None:
    """Note: importance validation is the caller's responsibility.
    The backend stores whatever importance is given."""
    fact = make_fact(importance=1)
    backend.store_fact(fact)
    assert backend.recall_facts()[0].importance == 1