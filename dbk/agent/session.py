"""Session manager for agent conversations."""
from __future__ import annotations

import uuid
from typing import Any

from dbk.agent.state import AgentState, WorkflowStage


class SessionManager:
    """Manages active agent sessions in memory."""

    def __init__(self) -> None:
        self._sessions: dict[str, AgentState] = {}

    def create_session(self, session_id: str | None = None, goal: str = "") -> AgentState:
        """Create a new session and return its initial state."""
        sid = session_id or str(uuid.uuid4())
        state = AgentState(
            session_id=sid,
            workflow_stage=WorkflowStage.REQUIREMENTS,
            workflow_goal=goal,
            intent="general",
        )
        self._sessions[sid] = state
        return state

    def get_session(self, session_id: str) -> AgentState | None:
        """Get an existing session by ID."""
        return self._sessions.get(session_id)

    def update_session(self, state: AgentState) -> None:
        """Update session state (replaces existing)."""
        self._sessions[state.session_id] = state

    def delete_session(self, session_id: str) -> bool:
        """Remove a session. Returns True if it existed."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def list_sessions(self) -> list[str]:
        """List all active session IDs."""
        return list(self._sessions.keys())

    def advance_workflow(self, session_id: str, target: WorkflowStage) -> AgentState:
        """Advance a session's workflow stage."""
        state = self._sessions.get(session_id)
        if state is None:
            raise KeyError(f"Session not found: {session_id}")
        new_state = state.advance_workflow(target)
        self._sessions[session_id] = new_state
        return new_state
