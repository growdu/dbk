"""Workflow state machine for agent task lifecycle."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from dbk.agent.state import AgentState, WorkflowStage

if TYPE_CHECKING:
    from dbk.agent.core import Agent


# Workflow state transition graph.
_WORKFLOW_NEXT: dict[WorkflowStage, WorkflowStage] = {
    WorkflowStage.REQUIREMENTS: WorkflowStage.DESIGN,
    WorkflowStage.DESIGN: WorkflowStage.IMPLEMENT,
    WorkflowStage.IMPLEMENT: WorkflowStage.TEST,
    WorkflowStage.TEST: WorkflowStage.RUNTIME,
    WorkflowStage.RUNTIME: WorkflowStage.DOC,
    WorkflowStage.DOC: WorkflowStage.OPS,
    WorkflowStage.OPS: WorkflowStage.DONE,
    WorkflowStage.DONE: WorkflowStage.REQUIREMENTS,
}

_WORKFLOW_DESCRIPTIONS: dict[WorkflowStage, str] = {
    WorkflowStage.REQUIREMENTS: "Gathering requirements and understanding the task",
    WorkflowStage.DESIGN: "Designing solution architecture and approach",
    WorkflowStage.IMPLEMENT: "Implementing the solution",
    WorkflowStage.TEST: "Testing and validating the implementation",
    WorkflowStage.RUNTIME: "Deploying to runtime environment",
    WorkflowStage.DOC: "Writing documentation and runbooks",
    WorkflowStage.OPS: "Operational handover and monitoring setup",
    WorkflowStage.DONE: "Task complete",
}

# Stage-specific tool routing: which tool names are most relevant per stage.
_STAGE_TOOL_ROUTING: dict[WorkflowStage, list[str]] = {
    WorkflowStage.REQUIREMENTS: [
        "collect_metrics", "query_metrics", "health_check", "validate_config",
    ],
    WorkflowStage.DESIGN: [
        "validate_config", "diagnose_incident", "query_metrics", "run_trace",
    ],
    WorkflowStage.IMPLEMENT: [
        "validate_config", "collect_metrics", "health_check", "daemon_status",
    ],
    WorkflowStage.TEST: [
        "collect_metrics", "query_metrics", "health_check", "diagnose_incident",
        "run_trace",
    ],
    WorkflowStage.RUNTIME: [
        "start_collector_daemon", "stop_collector_daemon", "daemon_status",
        "list_daemons", "health_check",
    ],
    WorkflowStage.DOC: [
        "cleanup_report", "daemon_status", "query_metrics", "list_daemons",
    ],
    WorkflowStage.OPS: [
        "cleanup_data", "cleanup_report", "daemon_status", "list_daemons",
        "validate_config",
    ],
    WorkflowStage.DONE: [],
}

# Stage-specific system prompts injected into the agent context.
STAGE_PROMPTS: dict[WorkflowStage, str] = {
    WorkflowStage.REQUIREMENTS: (
        "STAGE: Requirements Gathering. "
        "Focus on understanding what metrics, diagnostics, and observability data "
        "the user needs. Ask clarifying questions about instances, time ranges, "
        "and alert thresholds before proceeding."
    ),
    WorkflowStage.DESIGN: (
        "STAGE: Design. "
        "Design a monitoring/observability approach based on the gathered requirements. "
        "Recommend which collectors to run, which metrics to collect, and how to "
        "set up alerting and diagnostics pipelines."
    ),
    WorkflowStage.IMPLEMENT: (
        "STAGE: Implementation. "
        "Implement the designed solution: start collectors, configure metrics collection, "
        "set up diagnostic tools. Prefer safe defaults and validate each step."
    ),
    WorkflowStage.TEST: (
        "STAGE: Testing. "
        "Test the implementation by collecting metrics, running health checks, "
        "and verifying diagnostic paths. Validate that data is flowing correctly "
        "before proceeding to deployment."
    ),
    WorkflowStage.RUNTIME: (
        "STAGE: Runtime Deployment. "
        "Deploy the validated solution to the runtime environment. "
        "Start daemons, verify connectivity, and confirm metrics are being collected "
        "in production."
    ),
    WorkflowStage.DOC: (
        "STAGE: Documentation. "
        "Document the deployment: collector configuration, metric definitions, "
        "alert thresholds, runbooks, and troubleshooting procedures."
    ),
    WorkflowStage.OPS: (
        "STAGE: Operations Handover. "
        "Finalize operational readiness: confirm retention policies, cleanup schedules, "
        "monitoring dashboards, and runbooks are in place. Verify the system is "
        "self-operating."
    ),
    WorkflowStage.DONE: (
        "STAGE: Complete. "
        "All workflow stages have been completed. The task goal has been achieved. "
        "Summarize what was done and offer next steps."
    ),
}


class WorkflowStateMachine:
    """Manages workflow state transitions for agent tasks."""

    def __init__(self, initial: WorkflowStage = WorkflowStage.REQUIREMENTS) -> None:
        self._current: WorkflowStage = initial

    @property
    def current(self) -> WorkflowStage:
        return self._current

    @property
    def description(self) -> str:
        return _WORKFLOW_DESCRIPTIONS.get(self._current, "")

    def next(self) -> WorkflowStage:
        """Advance to the next workflow stage. Returns the new stage."""
        if self._current not in _WORKFLOW_NEXT:
            raise ValueError(f"No next stage defined for {self._current.value}")
        self._current = _WORKFLOW_NEXT[self._current]
        return self._current

    def goto(self, target: WorkflowStage) -> WorkflowStage:
        """Jump to a specific stage (with validation)."""
        # Allow backtracking (going to previous stages) but not arbitrary jumps forward.
        current_idx = list(WorkflowStage).index(self._current)
        target_idx = list(WorkflowStage).index(target)
        if target_idx > current_idx + 1:
            raise ValueError(
                f"Cannot jump forward from {self._current.value} to {target.value}. "
                f"Use next() to advance incrementally."
            )
        self._current = target
        return self._current

    def can_transition(self, target: WorkflowStage) -> bool:
        """Check if a transition is valid."""
        if target == _WORKFLOW_NEXT.get(self._current):
            return True
        # Allow jumping back.
        current_idx = list(WorkflowStage).index(self._current)
        target_idx = list(WorkflowStage).index(target)
        return target_idx <= current_idx

    def reset(self) -> WorkflowStage:
        """Reset to initial state."""
        self._current = WorkflowStage.REQUIREMENTS
        return self._current

    def is_done(self) -> bool:
        return self._current == WorkflowStage.DONE

    def get_stage_tools(self) -> list[str]:
        """Return the ranked list of tool names relevant for the current stage."""
        return _STAGE_TOOL_ROUTING.get(self._current, [])

    def get_stage_prompt(self) -> str:
        """Return the stage-specific system prompt."""
        return STAGE_PROMPTS.get(self._current, "")

    def progress_summary(self) -> dict[str, Any]:
        """Return a summary of current workflow progress."""
        stages = list(WorkflowStage)
        current_idx = stages.index(self._current)
        total = len(stages)
        return {
            "current": self._current.value,
            "description": self.description,
            "progress_pct": round((current_idx / max(total - 1, 1)) * 100, 1),
            "stage_number": current_idx + 1,
            "total_stages": total,
            "is_done": self.is_done(),
        }


def get_tools_for_stage(stage: WorkflowStage) -> list[str]:
    """Return the tool routing list for a given stage."""
    return _STAGE_TOOL_ROUTING.get(stage, [])


def get_prompt_for_stage(stage: WorkflowStage) -> str:
    """Return the stage-specific prompt for a given stage."""
    return STAGE_PROMPTS.get(stage, "")


class WorkflowOrchestrator:
    """High-level orchestrator that runs the agent through workflow stages.

    Provides:
    - stage_prompts: per-stage contextual prompts
    - auto_transition_on_completion: whether to auto-advance after a stage completes
    - run_stage(): execute a single stage with stage-aware tool routing
    - stage-specific tool routing via workflow state machine integration
    """

    def __init__(
        self,
        agent: "Agent",
        auto_transition_on_completion: bool = True,
    ) -> None:
        self._agent = agent
        self._auto_transition = auto_transition_on_completion
        self._stage_results: dict[WorkflowStage, dict[str, Any]] = {}

    @property
    def stage_prompts(self) -> dict[WorkflowStage, str]:
        """Return all stage prompts (read-only view)."""
        return STAGE_PROMPTS

    @property
    def auto_transition_on_completion(self) -> bool:
        return self._auto_transition

    @auto_transition_on_completion.setter
    def auto_transition_on_completion(self, value: bool) -> None:
        self._auto_transition = bool(value)

    def run_stage(
        self,
        message: str,
        target_stage: WorkflowStage,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a single workflow stage with stage-aware tool routing.

        Uses the agent's session (creating one if needed) and injects
        the stage-specific prompt alongside the message before processing.

        Returns a dict with keys: session_id, content, intent, tool_calls,
        tool_results, workflow_stage, stage_tools, auto_advanced.
        """
        # Resolve or create session.
        state = self._agent.get_session(session_id or "")
        if state is None:
            state = self._agent.create_session(session_id=session_id)

        # Get stage-aware context.
        stage_tools = get_tools_for_stage(target_stage)
        stage_prompt = get_prompt_for_stage(target_stage)

        # Build the user message with stage context.
        enhanced_message = (
            f"[{stage_prompt}]\n\n"
            f"[Stage tools available: {', '.join(stage_tools)}]\n\n"
            f"{message}"
        )

        # Process the message through the agent.
        result = self._agent.process_message(enhanced_message, session_id=state.session_id)

        # Record stage result.
        self._stage_results[target_stage] = {
            "message": message,
            "response": result,
            "stage_tools": stage_tools,
        }

        auto_advanced = False
        # Auto-transition if enabled and the agent's session advanced to the next stage.
        if self._auto_transition and state.workflow_stage != WorkflowStage.DONE:
            try:
                next_stage = self._next_stage(target_stage)
                if next_stage is not None:
                    self._agent.advance_workflow(state.session_id, next_stage)
                    auto_advanced = True
            except ValueError:
                # No valid transition from this stage, stay put.
                pass

        return {
            "session_id": result.get("session_id", state.session_id),
            "content": result.get("content", ""),
            "intent": result.get("intent", "general"),
            "tool_calls": result.get("tool_calls", []),
            "tool_results": result.get("tool_results", []),
            "workflow_stage": result.get("workflow_stage", target_stage.value),
            "stage_tools": stage_tools,
            "auto_advanced": auto_advanced,
        }

    def run_full_workflow(
        self,
        goal: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Run all workflow stages from REQUIREMENTS through DONE.

        Iteratively calls run_stage for each stage in sequence,
        returning a summary of all stage results.
        """
        state = self._agent.create_session(session_id=session_id, goal=goal)
        session_id_out = state.session_id

        summaries: list[dict[str, Any]] = []
        current = WorkflowStage.REQUIREMENTS

        while current != WorkflowStage.DONE:
            stage_tools = get_tools_for_stage(current)
            stage_prompt = get_prompt_for_stage(current)

            result = self._agent.process_message(
                f"[{stage_prompt}]\n[Stage tools: {', '.join(stage_tools)}]\n\n{goal}",
                session_id=session_id_out,
            )

            self._stage_results[current] = {
                "goal": goal,
                "response": result,
                "stage_tools": stage_tools,
            }

            summaries.append({
                "stage": current.value,
                "intent": result.get("intent", "general"),
                "tool_calls": result.get("tool_calls", []),
                "tool_results": result.get("tool_results", []),
            })

            # Attempt to advance to next stage.
            try:
                next_stage = self._next_stage(current)
                if next_stage is not None:
                    self._agent.advance_workflow(session_id_out, next_stage)
                    current = next_stage
                else:
                    break
            except ValueError:
                break

        return {
            "session_id": session_id_out,
            "workflow_goal": goal,
            "stages_completed": [s["stage"] for s in summaries],
            "stage_results": summaries,
            "total_stages": len(summaries),
        }

    def get_stage_result(self, stage: WorkflowStage) -> dict[str, Any] | None:
        """Return the cached result for a previously-run stage."""
        return self._stage_results.get(stage)

    def route_tool(
        self,
        tool_name: str,
        current_stage: WorkflowStage,
    ) -> dict[str, Any]:
        """Route a tool call through stage-aware context.

        Returns routing metadata: whether the tool is stage-appropriate,
        the tool's priority for this stage, and recommended alternatives.
        """
        stage_tools = get_tools_for_stage(current_stage)
        is_appropriate = tool_name in stage_tools

        # Compute priority: lower index = higher priority.
        try:
            priority = stage_tools.index(tool_name) + 1
        except ValueError:
            priority = len(stage_tools) + 1

        # Find alternative tools for this stage.
        alternatives = [t for t in stage_tools if t != tool_name]

        return {
            "tool_name": tool_name,
            "current_stage": current_stage.value,
            "is_appropriate": is_appropriate,
            "priority": priority,
            "stage_tools": stage_tools,
            "alternatives": alternatives,
            "recommendation": "use" if is_appropriate else "skip",
        }

    def _next_stage(self, current: WorkflowStage) -> WorkflowStage | None:
        """Return the next stage in the workflow, or None if at DONE."""
        from dbk.agent.state import _WORKFLOW_TRANSITIONS
        valid = _WORKFLOW_TRANSITIONS.get(current, [])
        if not valid:
            return None
        # Pick the most forward-looking valid transition.
        from dbk.agent.state import WorkflowStage as WS
        stages = list(WS)
        current_idx = stages.index(current)
        candidates = [s for s in valid if stages.index(s) > current_idx]
        if candidates:
            return min(candidates, key=lambda s: stages.index(s))
        # If no forward candidates, stay.
        return None
