"""Agent state definitions."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class WorkflowStage(str, Enum):
    """Workflow state machine stages."""

    REQUIREMENTS = "requirements"
    DESIGN = "design"
    IMPLEMENT = "implement"
    TEST = "test"
    RUNTIME = "runtime"
    DOC = "doc"
    OPS = "ops"
    DONE = "done"


# Valid transitions in the workflow state machine.
_WORKFLOW_TRANSITIONS: dict[WorkflowStage, list[WorkflowStage]] = {
    WorkflowStage.REQUIREMENTS: [WorkflowStage.DESIGN, WorkflowStage.DONE],
    WorkflowStage.DESIGN: [WorkflowStage.IMPLEMENT, WorkflowStage.REQUIREMENTS, WorkflowStage.DONE],
    WorkflowStage.IMPLEMENT: [WorkflowStage.TEST, WorkflowStage.DESIGN],
    WorkflowStage.TEST: [WorkflowStage.RUNTIME, WorkflowStage.IMPLEMENT],
    WorkflowStage.RUNTIME: [WorkflowStage.DOC, WorkflowStage.IMPLEMENT],
    WorkflowStage.DOC: [WorkflowStage.OPS, WorkflowStage.RUNTIME],
    WorkflowStage.OPS: [WorkflowStage.DONE, WorkflowStage.RUNTIME],
    WorkflowStage.DONE: [WorkflowStage.REQUIREMENTS],  # Can restart cycle
}


@dataclass(slots=True)
class AgentState:
    """Immutable-ish state snapshot for an agent session."""

    session_id: str
    workflow_stage: WorkflowStage = WorkflowStage.REQUIREMENTS
    workflow_goal: str = ""
    intent: str = "general"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    last_tool_result: dict[str, Any] | None = None
    conversation_history: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    turn_count: int = 0

    def advance_workflow(self, target: WorkflowStage) -> "AgentState":
        """Return a new AgentState with the workflow stage advanced if valid."""
        valid = _WORKFLOW_TRANSITIONS.get(self.workflow_stage, [])
        if target not in valid:
            raise ValueError(
                f"Invalid workflow transition: {self.workflow_stage.value} -> {target.value}. "
                f"Valid: {[s.value for s in valid]}"
            )
        import copy
        new_state = AgentState(
            session_id=self.session_id,
            workflow_stage=target,
            workflow_goal=self.workflow_goal,
            intent=self.intent,
            tool_calls=copy.deepcopy(self.tool_calls),
            last_tool_result=self.last_tool_result,
            conversation_history=copy.deepcopy(self.conversation_history),
            metadata=copy.copy(self.metadata),
            created_at=self.created_at,
            updated_at=_utc_now(),
            turn_count=self.turn_count,
        )
        return new_state

    def add_turn(self, user_input: str, agent_output: str) -> "AgentState":
        """Return a new AgentState with a conversation turn added."""
        import copy
        history = copy.deepcopy(self.conversation_history)
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": agent_output})
        return AgentState(
            session_id=self.session_id,
            workflow_stage=self.workflow_stage,
            workflow_goal=self.workflow_goal,
            intent=self.intent,
            tool_calls=copy.deepcopy(self.tool_calls),
            last_tool_result=self.last_tool_result,
            conversation_history=history,
            metadata=copy.copy(self.metadata),
            created_at=self.created_at,
            updated_at=_utc_now(),
            turn_count=self.turn_count + 1,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "workflow_stage": self.workflow_stage.value,
            "workflow_goal": self.workflow_goal,
            "intent": self.intent,
            "tool_calls": self.tool_calls,
            "last_tool_result": self.last_tool_result,
            "conversation_history": self.conversation_history,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turn_count": self.turn_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentState":
        return cls(
            session_id=data["session_id"],
            workflow_stage=WorkflowStage(data["workflow_stage"]),
            workflow_goal=data.get("workflow_goal", ""),
            intent=data.get("intent", "general"),
            tool_calls=data.get("tool_calls", []),
            last_tool_result=data.get("last_tool_result"),
            conversation_history=data.get("conversation_history", []),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", _utc_now()),
            updated_at=data.get("updated_at", _utc_now()),
            turn_count=data.get("turn_count", 0),
        )
