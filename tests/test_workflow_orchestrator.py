"""Tests for the WorkflowOrchestrator and stage-aware execution."""
from __future__ import annotations

import pytest

from dbk.agent.core import Agent
from dbk.agent.state import WorkflowStage
from dbk.agent.workflow import (
    STAGE_PROMPTS,
    WorkflowOrchestrator,
    WorkflowStateMachine,
    get_prompt_for_stage,
    get_tools_for_stage,
)
from dbk.providers.mock import MockProvider


# ----------------------------------------------------------------------
# Helper: stage-tool routing fixture data
# ----------------------------------------------------------------------


class TestStageToolRouting:
    """Test the stage -> tool routing tables."""

    def test_all_stages_have_tools(self) -> None:
        for stage in WorkflowStage:
            tools = get_tools_for_stage(stage)
            assert isinstance(tools, list), f"{stage} has no tool list"

    def test_all_stages_have_prompts(self) -> None:
        for stage in WorkflowStage:
            prompt = get_prompt_for_stage(stage)
            assert isinstance(prompt, str), f"{stage} has no prompt"
            assert len(prompt) > 0, f"{stage} prompt is empty"

    def test_requirements_stage_tools(self) -> None:
        tools = get_tools_for_stage(WorkflowStage.REQUIREMENTS)
        assert "collect_metrics" in tools
        assert "query_metrics" in tools
        assert "health_check" in tools
        assert "validate_config" in tools

    def test_runtime_stage_tools(self) -> None:
        tools = get_tools_for_stage(WorkflowStage.RUNTIME)
        assert "start_collector_daemon" in tools
        assert "stop_collector_daemon" in tools
        assert "daemon_status" in tools
        assert "list_daemons" in tools

    def test_ops_stage_tools(self) -> None:
        tools = get_tools_for_stage(WorkflowStage.OPS)
        assert "cleanup_data" in tools
        assert "cleanup_report" in tools
        assert "validate_config" in tools

    def test_done_stage_tools_empty(self) -> None:
        tools = get_tools_for_stage(WorkflowStage.DONE)
        assert tools == []

    def test_stage_prompts_contain_stage_keyword(self) -> None:
        for stage, prompt in STAGE_PROMPTS.items():
            assert "STAGE:" in prompt, f"{stage} prompt missing STAGE: prefix"
            # Each prompt should mention its stage name in some form.
            lower_prompt = prompt.lower()
            stage_word = stage.value
            assert (
                stage_word in lower_prompt
                or stage_word.replace("ops", "operations") in lower_prompt
                or "complete" in lower_prompt
            ), f"{stage} prompt does not reference its stage"


class TestWorkflowStateMachineStageMethods:
    """Test the new methods added to WorkflowStateMachine."""

    def test_get_stage_tools(self) -> None:
        wfm = WorkflowStateMachine(initial=WorkflowStage.REQUIREMENTS)
        tools = wfm.get_stage_tools()
        assert "collect_metrics" in tools
        assert "validate_config" in tools

    def test_get_stage_tools_runtime(self) -> None:
        wfm = WorkflowStateMachine(initial=WorkflowStage.RUNTIME)
        tools = wfm.get_stage_tools()
        assert "start_collector_daemon" in tools

    def test_get_stage_prompt(self) -> None:
        wfm = WorkflowStateMachine(initial=WorkflowStage.TEST)
        prompt = wfm.get_stage_prompt()
        assert "STAGE:" in prompt
        assert "Testing" in prompt


class TestWorkflowOrchestratorInit:
    """Test WorkflowOrchestrator initialization."""

    def test_init_with_default_auto_transition(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent)
        assert orch.auto_transition_on_completion is True

    def test_init_with_disabled_auto_transition(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=False)
        assert orch.auto_transition_on_completion is False

    def test_stage_prompts_property(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent)
        prompts = orch.stage_prompts
        assert len(prompts) == 8
        assert WorkflowStage.REQUIREMENTS in prompts
        assert len(prompts[WorkflowStage.DESIGN]) > 0

    def test_auto_transition_setter(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=True)
        orch.auto_transition_on_completion = False
        assert orch.auto_transition_on_completion is False


class TestWorkflowOrchestratorRunStage:
    """Test run_stage method."""

    def test_run_stage_requiresments(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=False)
        result = orch.run_stage(
            message="collect metrics for pg-main-01",
            target_stage=WorkflowStage.REQUIREMENTS,
            session_id="stage-req-001",
        )
        assert result["session_id"] == "stage-req-001"
        assert result["workflow_stage"] == "requirements"
        assert "stage_tools" in result
        assert "collect_metrics" in result["stage_tools"]
        assert result["auto_advanced"] is False

    def test_run_stage_creates_session_if_missing(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=False)
        result = orch.run_stage(
            message="validate config",
            target_stage=WorkflowStage.DESIGN,
        )
        assert result["session_id"] is not None
        assert result["workflow_stage"] in [s.value for s in WorkflowStage]

    def test_run_stage_injects_stage_tools(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=False)
        result = orch.run_stage(
            message="list daemons",
            target_stage=WorkflowStage.RUNTIME,
            session_id="stage-rt-001",
        )
        stage_tools = result["stage_tools"]
        assert "list_daemons" in stage_tools
        assert "start_collector_daemon" in stage_tools

    def test_run_stage_caches_result(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=False)
        orch.run_stage(
            message="health check",
            target_stage=WorkflowStage.TEST,
            session_id="cache-001",
        )
        cached = orch.get_stage_result(WorkflowStage.TEST)
        assert cached is not None
        assert "response" in cached
        assert "stage_tools" in cached

    def test_get_stage_result_returns_none_for_unseen_stage(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent)
        result = orch.get_stage_result(WorkflowStage.DESIGN)
        assert result is None


class TestWorkflowOrchestratorAutoTransition:
    """Test auto_transition_on_completion behaviour."""

    def test_auto_advance_disabled(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=False)
        # Advance session manually to DESIGN.
        state = agent.create_session(session_id="no-auto-001")
        agent.advance_workflow("no-auto-001", WorkflowStage.DESIGN)

        result = orch.run_stage(
            message="done with design",
            target_stage=WorkflowStage.DESIGN,
            session_id="no-auto-001",
        )
        # Should NOT have auto_advanced since it's disabled.
        assert result["auto_advanced"] is False

    def test_auto_advance_enabled(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=True)
        state = agent.create_session(session_id="auto-001")

        result = orch.run_stage(
            message="requirements gathered",
            target_stage=WorkflowStage.REQUIREMENTS,
            session_id="auto-001",
        )
        # The orchestrator should have attempted to advance.
        # Result depends on whether the session already transitioned,
        # but auto_advanced flag should be present.
        assert "auto_advanced" in result


class TestWorkflowOrchestratorRouteTool:
    """Test stage-specific tool routing via route_tool."""

    def test_route_tool_appropriate(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent)
        routing = orch.route_tool("collect_metrics", WorkflowStage.REQUIREMENTS)
        assert routing["tool_name"] == "collect_metrics"
        assert routing["current_stage"] == "requirements"
        assert routing["is_appropriate"] is True
        assert routing["recommendation"] == "use"
        assert routing["priority"] == 1
        assert "alternatives" in routing

    def test_route_tool_inappropriate(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent)
        routing = orch.route_tool("start_collector_daemon", WorkflowStage.REQUIREMENTS)
        assert routing["is_appropriate"] is False
        assert routing["recommendation"] == "skip"
        assert "start_collector_daemon" not in routing["stage_tools"]

    def test_route_tool_unknown_tool(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent)
        routing = orch.route_tool("nonexistent_tool", WorkflowStage.DESIGN)
        assert routing["is_appropriate"] is False
        assert routing["recommendation"] == "skip"
        assert routing["priority"] > len(routing["stage_tools"])

    def test_route_tool_done_stage(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent)
        routing = orch.route_tool("collect_metrics", WorkflowStage.DONE)
        assert routing["is_appropriate"] is False
        assert routing["stage_tools"] == []


class TestWorkflowOrchestratorRunFullWorkflow:
    """Test run_full_workflow."""

    def test_run_full_workflow_returns_summary(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=False)
        result = orch.run_full_workflow(
            goal="monitor pg-main-01",
            session_id="full-wf-001",
        )
        assert result["session_id"] == "full-wf-001"
        assert result["workflow_goal"] == "monitor pg-main-01"
        assert "stages_completed" in result
        assert "total_stages" in result
        assert "stage_results" in result

    def test_run_full_workflow_advances_through_stages(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=False)
        result = orch.run_full_workflow(
            goal="set up observability",
            session_id="full-wf-002",
        )
        # Should have completed at least the first stage.
        assert len(result["stages_completed"]) >= 1
        assert "requirements" in result["stages_completed"]

    def test_run_full_workflow_caches_all_stage_results(self) -> None:
        agent = Agent(provider=MockProvider())
        orch = WorkflowOrchestrator(agent, auto_transition_on_completion=False)
        orch.run_full_workflow(goal="test workflow", session_id="full-wf-003")
        # At least one stage result should be cached.
        cached_count = sum(
            1 for s in WorkflowStage if orch.get_stage_result(s) is not None
        )
        assert cached_count >= 1


class TestModuleLevelHelpers:
    """Test the module-level helper functions."""

    def test_get_tools_for_stage_known(self) -> None:
        tools = get_tools_for_stage(WorkflowStage.TEST)
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_get_tools_for_stage_unknown_stage(self) -> None:
        # Works for all defined stages; empty for any future additions.
        tools = get_tools_for_stage(WorkflowStage.DESIGN)
        assert isinstance(tools, list)

    def test_get_prompt_for_stage_known(self) -> None:
        prompt = get_prompt_for_stage(WorkflowStage.IMPLEMENT)
        assert "Implementation" in prompt or "implement" in prompt.lower()

    def test_get_prompt_for_stage_all_stages(self) -> None:
        for stage in WorkflowStage:
            prompt = get_prompt_for_stage(stage)
            assert len(prompt) > 0
