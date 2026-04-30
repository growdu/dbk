"""Workflow state machine for agent task lifecycle."""
from __future__ import annotations

from typing import Any

from dbk.agent.state import WorkflowStage


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
