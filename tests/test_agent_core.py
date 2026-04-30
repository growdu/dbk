"""Tests for the DBK AI Agent core."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from dbk.agent.core import Agent
from dbk.agent.intent import IntentRecognizer
from dbk.agent.session import SessionManager
from dbk.agent.session_store import SessionStore
from dbk.agent.state import AgentState, WorkflowStage
from dbk.agent.tools import Tool, ToolRegistry, tool_validate_config
from dbk.agent.workflow import WorkflowStateMachine
from dbk.providers.base import BaseProvider, CompletionMessage, CompletionResponse
from dbk.providers.mock import MockProvider


# ----------------------------------------------------------------------
# Mock Provider for tests.
# ----------------------------------------------------------------------


class EchoMockProvider(MockProvider):
    """Mock provider that echoes back structured info."""

    def __init__(self) -> None:
        super().__init__()

    def chat(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs,
    ) -> CompletionResponse:
        user_text = ""
        for msg in reversed(messages):
            if msg.role == "user":
                user_text = msg.content
                break

        return CompletionResponse(
            content=f"[echo] {user_text[:100]}",
            model="echo/mock",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            finish_reason="stop",
            raw={},
        )


# ----------------------------------------------------------------------
# Intent Recognizer tests.
# ----------------------------------------------------------------------


class TestIntentRecognizer:
    def test_keyword_collect_metrics(self) -> None:
        intent, params = IntentRecognizer().recognize("collect metrics for pg-main-01")
        assert intent == "collect_metrics"

    def test_keyword_query_metrics(self) -> None:
        intent, params = IntentRecognizer().recognize("show me latest cpu usage")
        assert intent == "query_metrics"

    def test_keyword_health_check(self) -> None:
        intent, params = IntentRecognizer().recognize("health check on pg-prod")
        assert intent == "health_check"

    def test_keyword_diagnose_incident(self) -> None:
        intent, params = IntentRecognizer().recognize("diagnose latency on pg-main-01")
        assert intent == "diagnose_incident"

    def test_keyword_cleanup_data(self) -> None:
        intent, params = IntentRecognizer().recognize("cleanup data older than 48 hours")
        assert intent == "cleanup_data"

    def test_keyword_start_daemon(self) -> None:
        intent, params = IntentRecognizer().recognize("start daemon for pg-main")
        assert intent == "start_daemon"

    def test_keyword_stop_daemon(self) -> None:
        intent, params = IntentRecognizer().recognize("stop the collector daemon")
        assert intent == "stop_daemon"

    def test_keyword_validate_config(self) -> None:
        intent, params = IntentRecognizer().recognize("validate config")
        assert intent == "validate_config"

    def test_keyword_general(self) -> None:
        intent, params = IntentRecognizer().recognize("what is the weather today")
        assert intent == "general"

    def test_params_instance_extraction(self) -> None:
        _, params = IntentRecognizer().recognize("query metrics for instance pg-backup-03")
        assert params.get("instance") == "pg-backup-03"

    def test_params_metric_extraction(self) -> None:
        _, params = IntentRecognizer().recognize("show memory usage")
        assert params.get("metric") == "memory"

    def test_params_dry_run_extraction(self) -> None:
        _, params = IntentRecognizer().recognize("cleanup data dry run")
        assert params.get("dry_run") is True


# ----------------------------------------------------------------------
# Workflow State Machine tests.
# ----------------------------------------------------------------------


class TestWorkflowStateMachine:
    def test_initial_stage(self) -> None:
        wfm = WorkflowStateMachine()
        assert wfm.current == WorkflowStage.REQUIREMENTS
        assert not wfm.is_done()

    def test_next_advances_in_order(self) -> None:
        wfm = WorkflowStateMachine()
        assert wfm.next() == WorkflowStage.DESIGN
        assert wfm.next() == WorkflowStage.IMPLEMENT
        assert wfm.next() == WorkflowStage.TEST
        assert wfm.next() == WorkflowStage.RUNTIME
        assert wfm.next() == WorkflowStage.DOC
        assert wfm.next() == WorkflowStage.OPS
        assert wfm.next() == WorkflowStage.DONE
        assert wfm.is_done()

    def test_next_from_done_restarts(self) -> None:
        wfm = WorkflowStateMachine(initial=WorkflowStage.DONE)
        assert wfm.next() == WorkflowStage.REQUIREMENTS

    def test_goto_backward_allowed(self) -> None:
        wfm = WorkflowStateMachine(initial=WorkflowStage.TEST)
        wfm.goto(WorkflowStage.DESIGN)
        assert wfm.current == WorkflowStage.DESIGN

    def test_goto_forward_not_allowed(self) -> None:
        wfm = WorkflowStateMachine(initial=WorkflowStage.REQUIREMENTS)
        with pytest.raises(ValueError, match="Cannot jump forward"):
            wfm.goto(WorkflowStage.IMPLEMENT)

    def test_can_transition(self) -> None:
        wfm = WorkflowStateMachine()
        assert wfm.can_transition(WorkflowStage.DESIGN)
        assert not wfm.can_transition(WorkflowStage.IMPLEMENT)

    def test_progress_summary(self) -> None:
        wfm = WorkflowStateMachine()
        summary = wfm.progress_summary()
        assert summary["current"] == "requirements"
        assert summary["stage_number"] == 1
        assert summary["total_stages"] == 8
        assert not summary["is_done"]


# ----------------------------------------------------------------------
# Session Manager tests.
# ----------------------------------------------------------------------


class TestSessionManager:
    def test_create_session(self) -> None:
        mgr = SessionManager()
        state = mgr.create_session(goal="monitor pg-main-01")
        assert state.session_id
        assert state.workflow_stage == WorkflowStage.REQUIREMENTS
        assert state.workflow_goal == "monitor pg-main-01"

    def test_create_with_id(self) -> None:
        mgr = SessionManager()
        state = mgr.create_session(session_id="my-session-123")
        assert state.session_id == "my-session-123"

    def test_get_session(self) -> None:
        mgr = SessionManager()
        state = mgr.create_session(session_id="test-001")
        retrieved = mgr.get_session("test-001")
        assert retrieved is not None
        assert retrieved.session_id == "test-001"

    def test_get_session_not_found(self) -> None:
        mgr = SessionManager()
        assert mgr.get_session("nonexistent") is None

    def test_update_session(self) -> None:
        mgr = SessionManager()
        state = mgr.create_session(session_id="test-002")
        new_state = state.advance_workflow(WorkflowStage.DESIGN)
        mgr.update_session(new_state)
        retrieved = mgr.get_session("test-002")
        assert retrieved is not None
        assert retrieved.workflow_stage == WorkflowStage.DESIGN

    def test_delete_session(self) -> None:
        mgr = SessionManager()
        mgr.create_session(session_id="to-delete")
        assert mgr.delete_session("to-delete")
        assert mgr.get_session("to-delete") is None
        assert not mgr.delete_session("already-gone")

    def test_list_sessions(self) -> None:
        mgr = SessionManager()
        mgr.create_session(session_id="s1")
        mgr.create_session(session_id="s2")
        sessions = mgr.list_sessions()
        assert set(sessions) == {"s1", "s2"}


# ----------------------------------------------------------------------
# Session Store tests.
# ----------------------------------------------------------------------


class TestSessionStore:
    def test_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "test.sqlite")
            state = AgentState(session_id="persist-001", workflow_goal="test task")
            store.save(state)

            loaded = store.load("persist-001")
            assert loaded is not None
            assert loaded.session_id == "persist-001"
            assert loaded.workflow_goal == "test task"

    def test_load_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "test.sqlite")
            assert store.load("nonexistent") is None

    def test_delete_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "test.sqlite")
            state = AgentState(session_id="to-remove")
            store.save(state)
            assert store.delete("to-remove")
            assert store.load("to-remove") is None
            assert not store.delete("nonexistent")

    def test_list_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "test.sqlite")
            for i in range(5):
                state = AgentState(session_id=f"list-{i:02d}")
                store.save(state)

            sessions = store.list_sessions()
            assert len(sessions) == 5

    def test_update_existing_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "test.sqlite")
            state = AgentState(session_id="update-001", workflow_goal="original")
            store.save(state)

            updated = state.advance_workflow(WorkflowStage.DESIGN)
            store.save(updated)

            loaded = store.load("update-001")
            assert loaded is not None
            assert loaded.workflow_stage == WorkflowStage.DESIGN
            assert loaded.workflow_goal == "original"


# ----------------------------------------------------------------------
# Tool Registry tests.
# ----------------------------------------------------------------------


class TestToolRegistry:
    def test_register_tool(self) -> None:
        registry = ToolRegistry()
        tool = Tool(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
            callable=lambda: "result",
        )
        registry.register(tool)
        assert registry.get("test_tool") is not None

    def test_tool_execute(self) -> None:
        registry = ToolRegistry()
        tool = registry.get("validate_config")
        assert tool is not None
        result = tool.execute()
        assert isinstance(result, dict)
        assert "ok" in result

    def test_tool_schemas(self) -> None:
        registry = ToolRegistry()
        schemas = registry.tool_schemas()
        names = {s["name"] for s in schemas}
        assert "validate_config" in names
        assert "collect_metrics" in names
        assert "query_metrics" in names
        assert len(schemas) >= 12  # At least 12 tools registered


# ----------------------------------------------------------------------
# Agent Core tests.
# ----------------------------------------------------------------------


class TestAgent:
    def test_agent_init_with_mock(self) -> None:
        agent = Agent(provider=MockProvider())
        info = agent.info()
        assert info["provider"] == "mock"
        assert info["tool_count"] >= 12

    def test_create_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "agent.sqlite")
            agent = Agent(provider=MockProvider(), session_store=store)
            state = agent.create_session(goal="monitor pg-main")
            assert state.session_id
            assert state.workflow_goal == "monitor pg-main"

            loaded = agent.get_session(state.session_id)
            assert loaded is not None

    def test_process_message_mock(self) -> None:
        agent = Agent(provider=MockProvider())
        result = agent.process_message("collect metrics for pg-main-01", session_id="test-sess")
        assert "content" in result
        assert result["session_id"] == "test-sess"
        assert result["workflow_stage"] == "requirements"
        assert "intent" in result

    def test_process_message_retains_history(self) -> None:
        agent = Agent(provider=MockProvider())
        result1 = agent.process_message("show me metrics", session_id="hist-test")
        result2 = agent.process_message("show me cpu", session_id="hist-test")
        assert result2["turn_count"] == 2

    def test_process_message_unknown_intent(self) -> None:
        agent = Agent(provider=MockProvider())
        result = agent.process_message("tell me a joke")
        assert "content" in result
        assert result["intent"] == "general"

    def test_process_stream(self) -> None:
        agent = Agent(provider=MockProvider())
        tokens = list(agent.process_stream("health check", session_id="stream-test"))
        full = "".join(tokens)
        assert full

    def test_advance_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "agent.sqlite")
            agent = Agent(provider=MockProvider(), session_store=store)
            state = agent.create_session(session_id="wfm-test")
            new_state = agent.advance_workflow("wfm-test", WorkflowStage.DESIGN)
            assert new_state.workflow_stage == WorkflowStage.DESIGN

    def test_advance_workflow_invalid_transition(self) -> None:
        agent = Agent(provider=MockProvider())
        agent.create_session(session_id="inv-trans")
        with pytest.raises(ValueError, match="Invalid workflow transition"):
            agent.advance_workflow("inv-trans", WorkflowStage.IMPLEMENT)

    def test_list_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "agent.sqlite")
            agent = Agent(provider=MockProvider(), session_store=store)
            agent.create_session(session_id="list-1")
            agent.create_session(session_id="list-2")
            sessions = agent.list_sessions()
            assert len(sessions) >= 2


# ----------------------------------------------------------------------
# Agent State tests.
# ----------------------------------------------------------------------


class TestAgentState:
    def test_state_to_dict_and_back(self) -> None:
        state = AgentState(session_id="dict-test", workflow_goal="test")
        data = state.to_dict()
        restored = AgentState.from_dict(data)
        assert restored.session_id == "dict-test"
        assert restored.workflow_goal == "test"

    def test_add_turn(self) -> None:
        state = AgentState(session_id="turn-test")
        new_state = state.add_turn("hello", "hi there!")
        assert len(new_state.conversation_history) == 2
        assert new_state.turn_count == 1

    def test_advance_workflow_valid(self) -> None:
        state = AgentState(session_id="adv-test")
        new_state = state.advance_workflow(WorkflowStage.DESIGN)
        assert new_state.workflow_stage == WorkflowStage.DESIGN

    def test_advance_workflow_invalid(self) -> None:
        state = AgentState(session_id="adv-fail")
        with pytest.raises(ValueError):
            state.advance_workflow(WorkflowStage.IMPLEMENT)
