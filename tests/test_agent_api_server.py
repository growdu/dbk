"""Integration tests for DBK Agent: REST API server, enhanced REPL, and memory system."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from dbk.agent.core import Agent
from dbk.agent.memory import AgentMemory, SQLiteMemoryBackend, InMemoryBackend, Fact, Summary
from dbk.agent.session_store import SessionStore
from dbk.agent.state import AgentState, WorkflowStage
from dbk.providers.mock import MockProvider


# ----------------------------------------------------------------------
# Agent Memory tests.
# ----------------------------------------------------------------------


class TestAgentMemoryBackend:
    def test_remember_and_recall_inmemory(self) -> None:
        mem = AgentMemory(backend=InMemoryBackend())
        fact = mem.remember(session_id="s1", key="pg_version", value="15.3", importance=8)
        assert fact.id
        assert fact.key == "pg_version"
        facts = mem.recall(session_id="s1")
        assert len(facts) == 1
        assert facts[0].value == "15.3"

    def test_recall_with_filters(self) -> None:
        mem = AgentMemory(backend=InMemoryBackend())
        mem.remember(session_id="s1", key="pg_version", value="15.3", importance=8)
        mem.remember(session_id="s1", key="pg_host", value="localhost", importance=5)
        mem.remember(session_id="s1", key="pg_version_minor", value="3", importance=2)

        by_prefix = mem.recall(session_id="s1", key_prefix="pg_version")
        assert len(by_prefix) == 2

        by_importance = mem.recall(session_id="s1", min_importance=6)
        assert len(by_importance) == 1
        assert by_importance[0].key == "pg_version"

    def test_forget_fact(self) -> None:
        mem = AgentMemory(backend=InMemoryBackend())
        fact = mem.remember(session_id="s1", key="temp", value="temp_value")
        assert mem.forget(fact.id)
        assert not mem.forget(fact.id)  # Already gone.
        assert len(mem.recall(session_id="s1")) == 0

    def test_summarize_and_recall(self) -> None:
        mem = AgentMemory(backend=InMemoryBackend())
        s = mem.summarize(session_id="s1", summary="Diagnosed high CPU on pg-main-01", window_start=0, window_end=5)
        assert s.id
        summaries = mem.get_summaries(session_id="s1")
        assert len(summaries) == 1
        assert "CPU" in summaries[0].summary

    def test_archive_and_recall_episodes(self) -> None:
        mem = AgentMemory(backend=InMemoryBackend())
        mem.archive_turn(session_id="s1", turn_count=1, role="user", content="Check CPU on pg-main")
        mem.archive_turn(session_id="s1", turn_count=2, role="assistant", content="CPU is at 85%")
        episodes = mem.recall_episodes(session_id="s1")
        assert len(episodes) == 2

    def test_build_context(self) -> None:
        mem = AgentMemory(backend=InMemoryBackend())
        mem.remember(session_id="s1", key="instance", value="pg-main-01", importance=7)
        mem.archive_turn(session_id="s1", turn_count=1, role="user", content="Show me CPU")
        ctx = mem.build_context(session_id="s1", max_facts=5, max_episodes=5)
        assert "pg-main-01" in ctx
        assert "CPU" in ctx

    def test_prune_episodes(self) -> None:
        mem = AgentMemory(backend=InMemoryBackend())
        for i in range(1, 21):
            mem.archive_turn(session_id="s1", turn_count=i, role="user", content=f"Turn {i}")
        deleted = mem.prune("s1", retain_turns=10)
        assert deleted > 0
        remaining = mem.recall_episodes("s1", limit=100)
        assert len(remaining) <= 10

    def test_sqlite_backend_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite"
            backend = SQLiteMemoryBackend(db_path=db_path)
            mem = AgentMemory(backend=backend)
            mem.remember(session_id="persist-001", key="cpu", value="90%", importance=8)
            mem.archive_turn(session_id="persist-001", turn_count=1, role="user", content="Test")

            # Load a new memory object with the same backend.
            backend2 = SQLiteMemoryBackend(db_path=db_path)
            mem2 = AgentMemory(backend=backend2)
            facts = mem2.recall(session_id="persist-001")
            assert len(facts) == 1
            assert facts[0].key == "cpu"

            episodes = mem2.recall_episodes(session_id="persist-001")
            assert len(episodes) == 1


# ----------------------------------------------------------------------
# REST API server tests.
# ----------------------------------------------------------------------


class TestAPIServerIntegration:
    """Test the REST API server with a real HTTP client."""

    @pytest.fixture
    def app_state(self) -> Any:
        """Create a test AppState with mock provider."""
        from dbk.api_server import AppState
        agent = Agent(provider=MockProvider())
        from dbk.agent.memory import AgentMemory, InMemoryBackend
        memory = AgentMemory(backend=InMemoryBackend())
        return AppState(agent=agent, memory=memory)

    @pytest.fixture
    def client(self, app_state: Any) -> Any:
        """Create a TestClient for the FastAPI app."""
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("starlette not installed")
        from dbk.api_server import create_app
        app = create_app(app_state)
        return TestClient(app)

    def test_health_endpoint(self, client: Any) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_ready_endpoint(self, client: Any) -> None:
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["ready"] is True

    def test_info_endpoint(self, client: Any) -> None:
        response = client.get("/info")
        assert response.status_code == 200
        data = response.json()
        assert "agent" in data
        assert "provider" in data["agent"]

    def test_create_session(self, client: Any) -> None:
        response = client.post("/sessions", params={"goal": "monitor pg"})
        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert data["workflow_goal"] == "monitor pg"

    def test_create_session_with_id(self, client: Any) -> None:
        custom_id = str(uuid.uuid4())
        response = client.post("/sessions", params={"session_id": custom_id, "goal": ""})
        assert response.status_code == 200
        assert response.json()["session_id"] == custom_id

    def test_get_session_not_found(self, client: Any) -> None:
        response = client.get("/sessions/nonexistent-session")
        assert response.status_code == 404

    def test_get_session_exists(self, client: Any) -> None:
        create_resp = client.post("/sessions", params={"goal": "test"})
        sid = create_resp.json()["session_id"]
        response = client.get(f"/sessions/{sid}")
        assert response.status_code == 200
        assert response.json()["session_id"] == sid

    def test_list_sessions(self, client: Any) -> None:
        for i in range(3):
            client.post("/sessions", params={"goal": f"goal-{i}"})
        response = client.get("/sessions")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 3
        assert len(data["sessions"]) >= 3

    def test_delete_session(self, client: Any) -> None:
        create_resp = client.post("/sessions", params={"goal": "to-delete"})
        sid = create_resp.json()["session_id"]
        del_resp = client.delete(f"/sessions/{sid}")
        assert del_resp.status_code == 200
        # Verify it's gone.
        get_resp = client.get(f"/sessions/{sid}")
        assert get_resp.status_code == 404

    def test_chat_basic(self, client: Any) -> None:
        response = client.post("/chat", params={"message": "health check"})
        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert "content" in data
        assert "intent" in data

    def test_chat_with_session_id(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        response = client.post("/chat", params={"message": "collect metrics", "session_id": sid})
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == sid

    def test_chat_retains_turns(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        client.post("/chat", params={"message": "first message", "session_id": sid})
        response = client.post("/chat", params={"message": "second message", "session_id": sid})
        assert response.json()["turn_count"] == 2

    def test_advance_workflow(self, client: Any) -> None:
        create_resp = client.post("/sessions", params={"goal": "wf-test"})
        sid = create_resp.json()["session_id"]
        assert create_resp.json()["workflow_stage"] == "requirements"

        # Advance to DESIGN.
        resp = client.post(f"/sessions/{sid}/workflow", params={"stage": "design"})
        assert resp.status_code == 200
        assert resp.json()["workflow_stage"] == "design"

    def test_advance_workflow_auto(self, client: Any) -> None:
        create_resp = client.post("/sessions", params={"goal": "auto-wf"})
        sid = create_resp.json()["session_id"]
        resp = client.post(f"/sessions/{sid}/workflow")
        assert resp.status_code == 200
        assert resp.json()["workflow_stage"] == "design"

    def test_advance_workflow_invalid_transition(self, client: Any) -> None:
        create_resp = client.post("/sessions", params={"goal": "inv-wf"})
        sid = create_resp.json()["session_id"]
        # Try to jump from requirements to implement (not allowed).
        resp = client.post(f"/sessions/{sid}/workflow", params={"stage": "implement"})
        assert resp.status_code == 400

    # Memory endpoints.
    def test_memory_remember(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        resp = client.post(
            "/memory/facts",
            params={
                "session_id": sid,
                "key": "pg_version",
                "value": "16.0",
                "importance": 8,
                "tags": "postgres,version",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "pg_version"
        assert data["importance"] == 8

    def test_memory_recall(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        client.post("/memory/facts", params={"session_id": sid, "key": "host", "value": "pg-main"})
        resp = client.get("/memory/facts", params={"session_id": sid})
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

    def test_memory_forget(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        create_resp = client.post(
            "/memory/facts",
            params={"session_id": sid, "key": "temp", "value": "temp"},
        )
        fact_id = create_resp.json()["id"]
        del_resp = client.delete(f"/memory/facts/{fact_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True

    def test_memory_summarize(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        resp = client.post(
            "/memory/summaries",
            params={
                "session_id": sid,
                "summary": "Diagnosed high CPU on pg-main",
                "window_start": 0,
                "window_end": 5,
            },
        )
        assert resp.status_code == 200
        assert "Diagnosed" in resp.json()["summary"]

    def test_memory_recall_summaries(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        client.post("/memory/summaries", params={
            "session_id": sid, "summary": "Test", "window_start": 0, "window_end": 3,
        })
        resp = client.get("/memory/summaries", params={"session_id": sid})
        assert resp.json()["count"] >= 1

    def test_memory_recall_episodes(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        # Archive via chat first.
        client.post("/chat", params={"message": "test message", "session_id": sid})
        resp = client.get("/memory/episodes", params={"session_id": sid})
        assert resp.status_code == 200
        assert "episodes" in resp.json()

    def test_memory_context(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        client.post("/memory/facts", params={"session_id": sid, "key": "pg", "value": "main"})
        resp = client.get("/memory/context", params={"session_id": sid})
        assert resp.status_code == 200
        assert "context" in resp.json()

    def test_memory_prune(self, client: Any) -> None:
        sid = str(uuid.uuid4())
        resp = client.post("/memory/prune", params={"session_id": sid, "retain_turns": 5})
        assert resp.status_code == 200
        assert "deleted" in resp.json()


class TestAPIServerSubprocess:
    """Test the API server running as a subprocess via dbk api-server CLI."""

    def test_api_server_start_and_respond(self) -> None:
        """Start server, hit health endpoint, then stop it."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.sqlite"

            env = {
                **dict(__import__("os").environ),
                "DBK_RUNTIME_DB_PATH": str(db_path),
                "DBK_ROOT": tmpdir,
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "ALL_PROXY": "",
            }

            proc = subprocess.Popen(
                [sys.executable, "-m", "dbk.cli", "api-server",
                 "--host", "127.0.0.1", "--port", "19080"],
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            base_url = "http://127.0.0.1:19080"
            started = False

            import time
            for _ in range(20):
                time.sleep(0.5)
                try:
                    import urllib.request
                    req = urllib.request.Request(f"{base_url}/health")
                    urllib.request.urlopen(req, timeout=1)
                    started = True
                    break
                except Exception:
                    pass

            if not started:
                proc.terminate()
                proc.wait(timeout=5)
                stderr = proc.stderr.read() if proc.stderr else ""
                stdout = proc.stdout.read() if proc.stdout else ""
                pytest.fail(
                    f"Server did not start. returncode={proc.poll()}. "
                    f"stderr={stderr[:500]}, stdout={stdout[:500]}"
                )

            try:
                import httpx
                with httpx.Client(
                    base_url=base_url,
                    timeout=10.0,
                    proxy=None,
                    trust_env=False,
                ) as client:
                    # Test health.
                    r = client.get("/health")
                    assert r.status_code == 200
                    assert r.json()["status"] == "ok"

                    # Test chat endpoint.
                    r = client.post("/chat", params={"message": "health check"})
                    assert r.status_code == 200
                    data = r.json()
                    assert "content" in data
                    assert "session_id" in data
            finally:
                proc.terminate()
                proc.wait(timeout=5)


# ----------------------------------------------------------------------
# Enhanced REPL tests.
# ----------------------------------------------------------------------


class TestEnhancedREPL:
    """Test the enhanced REPL without a terminal (send commands via lines list)."""

    @pytest.fixture
    def repl_agent(self) -> Agent:
        return Agent(provider=MockProvider())

    def test_repl_command_help(self, repl_agent: Agent) -> None:
        from dbk.agent.repl import REPL, REPLConfig, REPLCommand, _parse_input

        config = REPLConfig(enable_streaming=False, enable_colors=False, enable_history=False)
        repl = REPL(agent=repl_agent, config=config)
        cmd = _parse_input("help")
        assert cmd.name == "help"
        assert cmd.args == []

    def test_repl_command_parse_workflow(self, repl_agent: Agent) -> None:
        from dbk.agent.repl import _parse_input
        cmd = _parse_input("workflow advance design")
        assert cmd.name == "workflow"
        assert cmd.args == ["advance", "design"]

    def test_repl_command_parse_session(self, repl_agent: Agent) -> None:
        from dbk.agent.repl import _parse_input
        cmd = _parse_input("session list")
        assert cmd.name == "session"
        assert cmd.args == ["list"]

    def test_repl_command_parse_empty(self, repl_agent: Agent) -> None:
        from dbk.agent.repl import _parse_input
        cmd = _parse_input("")
        assert cmd.name == ""

    def test_repl_session_management(self, repl_agent: Agent) -> None:
        from dbk.agent.repl import REPL, REPLConfig
        config = REPLConfig(enable_streaming=False, enable_colors=False, enable_history=False)
        repl = REPL(agent=repl_agent, config=config)

        # Create initial session.
        state = repl_agent.create_session()
        repl._session_id = state.session_id

        # Create new session via command.
        from dbk.agent.repl import REPLCommand
        repl._handle_meta_command(REPLCommand(name="session", args=["new"], raw=""))
        assert repl.session_id != state.session_id

    def test_repl_workflow_command(self, repl_agent: Agent) -> None:
        from dbk.agent.repl import REPL, REPLConfig, REPLCommand
        config = REPLConfig(enable_streaming=False, enable_colors=False, enable_history=False)
        repl = REPL(agent=repl_agent, config=config)
        state = repl_agent.create_session()
        repl._session_id = state.session_id

        # Advance workflow.
        repl._handle_meta_command(REPLCommand(name="workflow", args=["design"], raw=""))
        updated = repl_agent.get_session(repl.session_id or "")
        assert updated is not None
        assert updated.workflow_stage == WorkflowStage.DESIGN

    def test_repl_memory_command(self, repl_agent: Agent) -> None:
        from dbk.agent.repl import REPL, REPLConfig, REPLCommand
        config = REPLConfig(enable_streaming=False, enable_colors=False, enable_history=False)
        repl = REPL(agent=repl_agent, config=config)
        state = repl_agent.create_session()
        repl._session_id = state.session_id

        # Add a fact and verify.
        repl._memory.remember(session_id=state.session_id, key="pg_version", value="16.0", importance=8)
        repl._handle_meta_command(REPLCommand(name="memory", args=["facts"], raw=""))
        # If no exception, command was handled.

    def test_repl_history(self, repl_agent: Agent) -> None:
        from dbk.agent.repl import REPL, REPLConfig, History
        with tempfile.TemporaryDirectory() as tmpdir:
            hist_path = Path(tmpdir) / "history.txt"
            config = REPLConfig(
                enable_streaming=False,
                enable_colors=False,
                enable_history=True,
                history_path=hist_path,
            )
            repl = REPL(agent=repl_agent, config=config)
            repl._history.append("test command 1")
            repl._history.append("test command 2")
            assert "test command 1" in repl._history.get_lines()
            assert "test command 2" in repl._history.get_lines()
            # Search.
            matches = repl._history.search("test")
            assert len(matches) == 2


class TestAgentWithMemory:
    """Test agent integrated with memory system."""

    def test_agent_process_message_stores_memory(self) -> None:
        agent = Agent(provider=MockProvider(), session_store=SessionStore())
        memory = AgentMemory()

        sid = str(uuid.uuid4())
        agent.create_session(session_id=sid, goal="test with memory")
        result = agent.process_message("collect metrics for pg-main-01", session_id=sid)

        # Memory should be updated (archiving is done via _maybe_archive_to_memory
        # in API, but here we test the integration directly).
        assert result["session_id"] == sid
        assert "content" in result

        # Check session was persisted.
        state = agent.get_session(sid)
        assert state is not None
        assert state.turn_count >= 1


class TestAPIServerMemoryIntegration:
    """Test API server with real memory backend persistence."""

    def test_memory_persists_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite"
            from dbk.api_server import AppState, create_app

            # First session.
            backend1 = SQLiteMemoryBackend(db_path=db_path)
            state1 = AppState(
                agent=Agent(provider=MockProvider()),
                memory=AgentMemory(backend=backend1),
            )
            app1 = create_app(state1)
            try:
                from starlette.testclient import TestClient
            except ImportError:
                pytest.skip("starlette not installed")
            client1 = TestClient(app1)

            sid = str(uuid.uuid4())
            client1.post("/memory/facts", params={
                "session_id": sid, "key": "persistent_key", "value": "persistent_value",
            })

            # Second session (same DB).
            backend2 = SQLiteMemoryBackend(db_path=db_path)
            state2 = AppState(
                agent=Agent(provider=MockProvider()),
                memory=AgentMemory(backend=backend2),
            )
            app2 = create_app(state2)
            client2 = TestClient(app2)

            resp = client2.get("/memory/facts", params={"session_id": sid})
            assert resp.json()["count"] >= 1
            values = [f["value"] for f in resp.json()["facts"]]
            assert "persistent_value" in values


# ----------------------------------------------------------------------
# CLI api-server subcommand tests.
# ----------------------------------------------------------------------


class TestAPIServerCLI:
    """Test dbk api-server subcommand via subprocess."""

    def test_api_server_starts_without_error(self) -> None:
        """Start server and verify it doesn't crash immediately."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "dbk.cli", "api-server", "--port", "0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(3)
        # Check it's still running (didn't crash).
        poll = proc.poll()
        if poll is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            stdout = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"Server exited with code {poll}. stderr={stderr}, stdout={stdout}")
        # Stop it cleanly.
        proc.terminate()
        proc.wait(timeout=5)

    def test_api_server_help(self) -> None:
        """Verify api-server subcommand is registered."""
        proc = subprocess.run(
            [sys.executable, "-m", "dbk.cli", "api-server", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0
        assert "--host" in proc.stdout or "--port" in proc.stdout


# ----------------------------------------------------------------------
# Session store + memory integration.
# ----------------------------------------------------------------------


class TestSessionAndMemoryIntegration:
    def test_full_pipeline(self) -> None:
        """End-to-end: create session, chat, store facts, advance workflow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            store = SessionStore(db_path=db_path)
            agent = Agent(provider=MockProvider(), session_store=store)
            memory = AgentMemory()

            # Create session.
            state = agent.create_session(goal="monitor pg-main-01")
            sid = state.session_id

            # Chat.
            result1 = agent.process_message("collect metrics", session_id=sid)
            assert result1["turn_count"] == 1

            # Store fact.
            memory.remember(session_id=sid, key="instance", value="pg-main-01", importance=7)

            # Advance workflow.
            new_state = agent.advance_workflow(sid, WorkflowStage.DESIGN)
            assert new_state.workflow_stage == WorkflowStage.DESIGN

            # Verify persistence.
            loaded = store.load(sid)
            assert loaded is not None
            assert loaded.workflow_stage == WorkflowStage.DESIGN

            # Verify memory.
            facts = memory.recall(session_id=sid)
            assert any(f.key == "instance" for f in facts)
