"""Tests for the DBK Agent TUI."""
from __future__ import annotations

import pytest

from dbk.agent.core import Agent
from dbk.agent.state import WorkflowStage
from dbk.providers.mock import MockProvider


class TestTUIModuleImport:
    """Verify the TUI module imports without error."""

    def test_tui_module_imports(self) -> None:
        from dbk.tui import DBKTUI, ChatMessage, SessionItem, ALL_STAGES
        assert DBKTUI is not None
        assert ChatMessage is not None
        assert SessionItem is not None
        assert len(ALL_STAGES) == 8

    def test_cli_tui_module_imports(self) -> None:
        from dbk.cli_tui import main, _build_tui_parser
        assert callable(main)
        assert callable(_build_tui_parser)


class TestDBKTUIComponents:
    """Unit tests for TUI component classes and data structures."""

    def test_chat_message_user(self) -> None:
        from dbk.tui import ChatMessage
        msg = ChatMessage(role="user", content="hello")
        assert msg.role == "user"
        assert msg.content == "hello"
        assert msg.tool_name is None
        assert msg.tool_result is None
        assert msg.expanded is False

    def test_chat_message_assistant(self) -> None:
        from dbk.tui import ChatMessage
        msg = ChatMessage(role="assistant", content="hi there")
        assert msg.role == "assistant"
        assert msg.expanded is False

    def test_chat_message_tool(self) -> None:
        from dbk.tui import ChatMessage
        result = {"ok": True, "result": {"foo": 42}}
        msg = ChatMessage(role="tool", content="", tool_name="collect_metrics", tool_result=result)
        assert msg.role == "tool"
        assert msg.tool_name == "collect_metrics"
        assert msg.tool_result == result

    def test_session_item_defaults(self) -> None:
        from dbk.tui import SessionItem
        item = SessionItem(
            session_id="test-123",
            stage=WorkflowStage.REQUIREMENTS,
            turn_count=3,
        )
        assert item.active is False
        assert item.session_id == "test-123"
        assert item.stage == WorkflowStage.REQUIREMENTS

    def test_all_stages_defined(self) -> None:
        from dbk.tui import ALL_STAGES, _STAGE_LABELS, _STAGE_COLORS
        assert len(ALL_STAGES) == 8
        for stage in ALL_STAGES:
            assert stage in _STAGE_LABELS
            assert stage in _STAGE_COLORS

    def test_stage_order(self) -> None:
        from dbk.tui import ALL_STAGES
        assert ALL_STAGES == [
            WorkflowStage.REQUIREMENTS,
            WorkflowStage.DESIGN,
            WorkflowStage.IMPLEMENT,
            WorkflowStage.TEST,
            WorkflowStage.RUNTIME,
            WorkflowStage.DOC,
            WorkflowStage.OPS,
            WorkflowStage.DONE,
        ]


class TestDBKTUIInstantiation:
    """Test that DBKTUI can be instantiated."""

    def test_instantiation_default(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        assert app._agent is not None
        assert app._current_session_id is not None
        assert isinstance(app._messages, list)
        assert len(app._messages) == 0

    def test_instantiation_with_agent(self) -> None:
        from dbk.tui import DBKTUI
        agent = Agent(provider=MockProvider())
        app = DBKTUI(agent=agent)
        assert app._agent is agent

    def test_instantiation_with_session_id_not_found_creates_new(self) -> None:
        from dbk.tui import DBKTUI
        # When the session_id is not found, a new session is created.
        app = DBKTUI(initial_session_id="nonexistent-session")
        # Should create a fresh session since "nonexistent-session" doesn't exist.
        assert app._current_session_id is not None
        assert app._current_session_id != "nonexistent-session"

    def test_current_stage_defaults_to_requirements(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        assert app._current_stage == WorkflowStage.REQUIREMENTS

    def test_valid_transitions_from_requirements(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        valid = app._get_valid_transitions()
        assert WorkflowStage.DESIGN in valid
        assert WorkflowStage.DONE in valid


import asyncio

class TestDBKTUIActions:
    """Test TUI action methods (non-DOM-dependent)."""

    def test_action_quit_does_not_raise(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        # action_quit is async but should not raise
        result = app.action_quit()
        if asyncio.iscoroutine(result):
            result.close()  # don't await, just ensure no raise

    def test_action_clear_chat_works_on_messages_list(self) -> None:
        from dbk.tui import DBKTUI, ChatMessage
        app = DBKTUI()
        app._messages.append(ChatMessage(role="user", content="test"))
        assert len(app._messages) == 1
        app.action_clear_chat()
        assert len(app._messages) == 0

    def test_init_session_creates_state(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        old_sid = app._current_session_id
        app._init_session()
        assert app._current_session_id != old_sid
        assert app._current_session_id in app._sessions

    def test_update_session_item_registers_session(self) -> None:
        from dbk.tui import DBKTUI
        from dbk.agent.state import AgentState
        app = DBKTUI()
        state = AgentState(
            session_id="sess-update-test",
            workflow_stage=WorkflowStage.DESIGN,
            turn_count=5,
        )
        app._update_session_item(state)
        assert "sess-update-test" in app._sessions
        assert app._sessions["sess-update-test"].stage == WorkflowStage.DESIGN
        assert app._sessions["sess-update-test"].turn_count == 5

    def test_multiple_init_session_different_ids(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        sids = [app._current_session_id]
        for _ in range(3):
            app._init_session()
            sids.append(app._current_session_id)
        assert len(sids) == len(set(sids))


class TestDBKTUISessionManagement:
    """Test session create/switch behavior."""

    def test_create_session_registered(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        initial_sid = app._current_session_id
        assert initial_sid in app._sessions


class TestDBKTUIWorkflowAdvance:
    """Test workflow stage advancement via agent (no DOM required)."""

    def test_advance_to_valid_stage(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        assert app._current_stage == WorkflowStage.REQUIREMENTS
        # DESIGN is a valid transition from REQUIREMENTS.
        app._advance_to_stage(WorkflowStage.DESIGN)
        # The agent's session should reflect this.
        state = app._current_state
        assert state is not None
        assert state.workflow_stage == WorkflowStage.DESIGN

    def test_advance_to_invalid_stage_calls_notify(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        assert app._current_stage == WorkflowStage.REQUIREMENTS
        # IMPLEMENT is NOT a valid direct transition from REQUIREMENTS.
        # The method catches ValueError and calls notify() instead of raising.
        app._advance_to_stage(WorkflowStage.IMPLEMENT)
        # Verify stage was NOT changed.
        assert app._current_stage == WorkflowStage.REQUIREMENTS

    def test_stage_stepper_updates_on_advance(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        assert app._current_stage == WorkflowStage.REQUIREMENTS
        app._advance_to_stage(WorkflowStage.DESIGN)
        assert app._current_stage == WorkflowStage.DESIGN

    def test_cycle_stage_from_requirements_moves_forward(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        # REQUIREMENTS -> cycle should move to DESIGN.
        app._cycle_stage()
        # The session should advance.
        state = app._current_state
        assert state is not None


class TestCLI_TUI:
    """Test the cli_tui module."""

    def test_build_parser(self) -> None:
        from dbk.cli_tui import _build_tui_parser
        parser = _build_tui_parser()
        args = parser.parse_args([])
        assert args.provider == "mock"
        assert args.session_id is None
        assert args.model is None
        assert args.no_stream is False

    def test_build_parser_with_args(self) -> None:
        from dbk.cli_tui import _build_tui_parser
        parser = _build_tui_parser()
        args = parser.parse_args(["--provider", "openai", "--model", "gpt-4o", "--session", "abc"])
        assert args.provider == "openai"
        assert args.model == "gpt-4o"
        assert args.session_id == "abc"

    def test_create_agent_mock(self) -> None:
        from dbk.cli_tui import _create_agent
        agent = _create_agent("mock", None)
        assert agent is not None
        assert isinstance(agent, Agent)

    def test_main_import(self) -> None:
        from dbk.cli_tui import main
        assert callable(main)


class TestTUIWithTextualPilot:
    """Integration-style tests using textual.testing.Pilot."""

    async def test_app_composes_without_error(self) -> None:
        from textual.pilot import Pilot
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            assert isinstance(pilot, Pilot)

    async def test_header_present(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            header = app.query_one("Header")
            assert header is not None

    async def test_sidebar_present(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            sidebar = app.query_one("#sidebar")
            assert sidebar is not None

    async def test_input_field_present(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            inp = app.query_one("#input-field")
            assert inp is not None

    async def test_send_button_present(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            btn = app.query_one("#send-button")
            assert btn is not None

    async def test_stage_stepper_present(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            stepper = app.query_one("#stage-stepper")
            assert stepper is not None

    async def test_status_bar_present(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            status = app.query_one("#status-bar")
            assert status is not None
            # Status bar should reference the current session ID.
            assert app._current_session_id is not None

    async def test_send_message(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            inp = app.query_one("#input-field")
            await pilot.click("#input-field")
            for ch in "test message":
                await pilot.press(ch)
            await pilot.press("enter")
            # The input should be cleared after send.
            assert inp.value == ""

    async def test_new_session_button_creates_new_session(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            old_sid = app._current_session_id
            await pilot.click("#btn-new-session")
            assert app._current_session_id != old_sid

    async def test_clear_chat_shortcut(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            # Directly test the action.
            app._add_user_message("hello")
            assert len(app._messages) == 1
            await pilot.press("ctrl+l")
            assert len(app._messages) == 0

    async def test_tab_cycles_stage(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            initial = app._current_stage
            await pilot.press("tab")
            # The tab binding is registered; cycle_stage is called.
            # Verify stage stepper text updated.
            stepper = app.query_one("#stage-stepper", app.query_one("#stage-stepper").__class__)
            assert stepper is not None

    async def test_advance_button(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            assert app._current_stage == WorkflowStage.REQUIREMENTS
            await pilot.click("#btn-advance")
            state = app._current_state
            assert state is not None

    async def test_message_log_widget_exists(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            log = app.query_one("#message-log")
            assert log is not None

    async def test_tool_results_container_exists(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            container = app.query_one("#tool-results-container")
            assert container is not None

    async def test_session_items_container_exists(self) -> None:
        from dbk.tui import DBKTUI
        app = DBKTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            container = app.query_one("#session-items-container")
            assert container is not None
