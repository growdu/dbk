"""Textual TUI for the DBK Agent.

Layout:
  [Header: title + provider/model]
  [Sidebar 25%]    |  [Main chat area]     [Status bar]
  - Stage stepper  |  - Message list      session_id
  - Session list   |  - Tool result panels turn_count
  - Action buttons |  - Auto-scroll        stage
  [Input: multi-line + Send button + shortcuts]

Keyboard shortcuts:
  Ctrl+Q  quit
  Ctrl+L  clear chat
  Tab     cycle workflow stages
"""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Generator

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Static,
)

from dbk.agent.core import Agent
from dbk.agent.state import AgentState, WorkflowStage
from dbk.providers.mock import MockProvider

# All workflow stages in order.
ALL_STAGES = list(WorkflowStage)

# Stage display names and descriptions.
_STAGE_LABELS: dict[WorkflowStage, str] = {
    WorkflowStage.REQUIREMENTS: "Requirements",
    WorkflowStage.DESIGN: "Design",
    WorkflowStage.IMPLEMENT: "Implement",
    WorkflowStage.TEST: "Test",
    WorkflowStage.RUNTIME: "Runtime",
    WorkflowStage.DOC: "Doc",
    WorkflowStage.OPS: "Ops",
    WorkflowStage.DONE: "Done",
}

_STAGE_COLORS: dict[WorkflowStage, str] = {
    WorkflowStage.REQUIREMENTS: "cyan",
    WorkflowStage.DESIGN: "blue",
    WorkflowStage.IMPLEMENT: "green",
    WorkflowStage.TEST: "yellow",
    WorkflowStage.RUNTIME: "magenta",
    WorkflowStage.DOC: "white",
    WorkflowStage.OPS: "cyan",
    WorkflowStage.DONE: "green",
}


@dataclass
class ChatMessage:
    """A single chat message."""
    role: str          # "user" | "assistant" | "tool"
    content: str
    tool_name: str | None = None
    tool_result: dict[str, Any] | None = None
    expanded: bool = False   # for tool result panels


@dataclass
class SessionItem:
    """A session entry for the sidebar list."""
    session_id: str
    stage: WorkflowStage
    turn_count: int
    active: bool = False


# ---------------------------------------------------------------------------
# Custom widgets
# ---------------------------------------------------------------------------


class StageStepper(Static):
    """Vertical stepper showing all 8 workflow stages."""

    current_stage = reactive(WorkflowStage.REQUIREMENTS)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._update_content()

    def _update_content(self) -> None:
        lines = ["[b]Workflow[/b]", ""]
        for stage in ALL_STAGES:
            label = _STAGE_LABELS[stage]
            marker = ">"
            if stage == self.current_stage:
                color = _STAGE_COLORS[stage]
                lines.append(f"[{color} bold]{marker} {label}[/{color} bold]")
            else:
                dim = ALL_STAGES.index(stage) < ALL_STAGES.index(self.current_stage)
                if dim:
                    lines.append(f"[dim]  {label}[/dim]")
                else:
                    lines.append(f"[dim]  {label}[/dim]")
        self.update("\n".join(lines))

    def watch_current_stage(self, stage: WorkflowStage) -> None:
        self._update_content()


class ToolResultPanel(Static):
    """Collapsible JSON viewer for a single tool result."""

    tool_name: str
    data: dict[str, Any]
    expanded: bool = False

    def __init__(self, tool_name: str, data: dict[str, Any], **kwargs: Any) -> None:
        self.tool_name = tool_name
        self.data = data
        super().__init__("", **kwargs)
        self._update_text()

    def _update_text(self) -> None:
        marker = "- " if self.expanded else "+ "
        json_str = json.dumps(self.data, indent=2)
        # Truncate long output for display
        if len(json_str) > 600:
            json_str = json_str[:600] + "\n... (truncated)"
        self.update(f"[bold cyan]{marker}[/bold cyan] [b]{self.tool_name}[/b]\n[dim]{json_str}[/dim]")

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._update_text()


class SessionListItem(Static):
    """A single clickable session entry in the sidebar."""

    session: SessionItem

    def __init__(self, session: SessionItem, **kwargs: Any) -> None:
        self.session = session
        super().__init__("", **kwargs)
        self._update_text()

    def _update_text(self) -> None:
        sid = self.session.session_id[:8]
        stage = _STAGE_LABELS[self.session.stage]
        color = _STAGE_COLORS[self.session.stage]
        active_marker = "[bold white]*[/bold white] " if self.session.active else "  "
        self.update(
            f"{active_marker}[{color}]{sid}[/{color}]\n"
            f"  [dim]{stage} | turn {self.session.turn_count}[/dim]"
        )

    def watch_session(self, session: SessionItem) -> None:
        self._update_text()


class MessageLog(Static):
    """Scrolling message list for the chat area."""

    messages: list[ChatMessage] = field(default_factory=list)

    def __init__(self, **kwargs: Any) -> None:
        # Override the dataclass field with a plain instance variable so that
        # access in __init__ (before __post_init__ runs) returns an actual list
        # rather than the Field descriptor object.
        self.messages: list[ChatMessage] = []
        super().__init__("", **kwargs)
        self._update_text()

    def add_message(self, msg: ChatMessage) -> None:
        self.messages.append(msg)
        self._update_text()

    def clear_messages(self) -> None:
        self.messages.clear()
        self._update_text()

    def update_last_message_content(self, content: str) -> None:
        """Append to the last assistant message (streaming)."""
        if self.messages and self.messages[-1].role == "assistant":
            self.messages[-1].content += content
        self._update_text()

    def _update_text(self) -> None:
        if not self.messages:
            self.update("[dim]Send a message to start the conversation...[/dim]")
            return

        parts: list[str] = []
        for msg in self.messages:
            if msg.role == "user":
                parts.append(f"\n[bold cyan]> You:[/bold cyan]\n{msg.content}\n")
            elif msg.role == "tool":
                result_str = json.dumps(msg.tool_result or {}, indent=2)
                if len(result_str) > 500:
                    result_str = result_str[:500] + "\n... (truncated)"
                parts.append(
                    f"\n[bold yellow]  [{msg.tool_name}][/bold yellow]\n"
                    f"[dim]{result_str}[/dim]\n"
                )
            else:
                parts.append(f"\n[bold green> DBK Agent:[/bold green]\n{msg.content}\n")

        self.update("\n".join(parts))


# ---------------------------------------------------------------------------
# Main TUI App
# ---------------------------------------------------------------------------


class DBKTUI(App):
    """Textual TUI for the DBK Agent."""

    TITLE = "DBK Agent"

    CSS = """
    Screen {
        background: $surface;
    }

    #layout {
        width: 100%;
        height: 100%;
    }

    #sidebar {
        width: 26;
        height: 100%;
        background: $panel;
        border-right: solid $border;
        padding: 1;
    }

    #main-area {
        width: 1fr;
        height: 100%;
    }

    #chat-area {
        height: 1fr;
        padding: 1 2;
    }

    #input-area {
        height: auto;
        border-top: solid $border;
        padding: 1 2;
        background: $panel;
    }

    #status-bar {
        height: 1;
        background: $surface;
        border-top: solid $border;
        padding: 0 2;
        content-align: left middle;
    }

    #stage-stepper {
        margin-bottom: 1;
    }

    #session-list-title {
        margin-bottom: 1;
    }

    #session-list {
        height: 30%;
        margin-bottom: 1;
    }

    #action-buttons {
        height: auto;
        layout: horizontal;
    }

    #message-log {
        height: 1fr;
    }

    #tool-results {
        height: auto;
        max-height: 40%;
    }

    #input-field {
        width: 1fr;
        height: 3;
    }

    #send-button {
        width: 12;
        margin-left: 1;
    }

    #status-text {
        width: 100%;
        content-align: left middle;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear"),
        ("tab", "cycle_stage", "Cycle Stage"),
    ]

    def __init__(
        self,
        agent: Agent | None = None,
        initial_session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._agent = agent or Agent(provider=MockProvider())
        self._current_session_id: str | None = initial_session_id
        self._sessions: dict[str, SessionItem] = {}
        self._messages: list[ChatMessage] = []
        self._streaming_task: asyncio.Task[None] | None = None
        self._typewriter_delay: float = 0.015

        # Ensure we have a session.
        if self._current_session_id:
            state = self._agent.get_session(self._current_session_id)
            if state is None:
                self._init_session()
        else:
            self._init_session()

    def _init_session(self) -> None:
        """Create a fresh session and register it."""
        state = self._agent.create_session()
        self._current_session_id = state.session_id
        self._update_session_item(state)

    def _update_session_item(self, state: AgentState) -> None:
        """Update the session list item for a given session."""
        item = SessionItem(
            session_id=state.session_id,
            stage=state.workflow_stage,
            turn_count=state.turn_count,
            active=(state.session_id == self._current_session_id),
        )
        self._sessions[state.session_id] = item

    @property
    def _current_state(self) -> AgentState | None:
        if self._current_session_id:
            return self._agent.get_session(self._current_session_id)
        return None

    @property
    def _current_stage(self) -> WorkflowStage:
        state = self._current_state
        return state.workflow_stage if state else WorkflowStage.REQUIREMENTS

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Header with title and provider info.
        info = self._agent.info()
        self.sub_title = f"{info['provider']} / {info.get('model', '?')}"
        yield Header()

        # Sidebar + main area.
        with Horizontal(id="layout"):
            # Left sidebar.
            with Vertical(id="sidebar"):
                # Workflow stage stepper.
                yield StageStepper(id="stage-stepper")

                # Session list title.
                yield Static("[b]Sessions[/b]", id="session-list-title")

                # Session list (scrollable).
                with VerticalScroll(id="session-list"):
                    yield Vertical(id="session-items-container")

                # Action buttons.
                with Horizontal(id="action-buttons"):
                    yield Button("New Session", id="btn-new-session")
                    yield Button("Advance", id="btn-advance")
                    yield Button("Back", id="btn-back")

            # Main area.
            with Vertical(id="main-area"):
                # Chat messages.
                with VerticalScroll(id="chat-area"):
                    yield MessageLog(id="message-log")

                # Tool results (collapsible panels shown after tools run).
                with VerticalScroll(id="tool-results-area"):
                    yield Vertical(id="tool-results-container")

                # Input area.
                with Horizontal(id="input-area"):
                    yield Input(
                        placeholder="Send a message... (Enter=send, Shift+Enter=newline)",
                        id="input-field",
                    )
                    yield Button("Send", id="send-button", variant="primary")

        # Status bar at bottom of main area.
        yield Static(id="status-bar")

    def on_mount(self) -> None:
        self._refresh_status()
        self._refresh_session_list()
        self._refresh_stage_stepper()
        self._focus_input()

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _focus_input(self) -> None:
        input_widget = self.query_one("#input-field", Input)
        input_widget.focus()

    def _refresh_status(self) -> None:
        state = self._current_state
        sid = self._current_session_id or "—"
        turns = state.turn_count if state else 0
        stage = _STAGE_LABELS.get(state.workflow_stage if state else WorkflowStage.REQUIREMENTS, "—")
        color = _STAGE_COLORS.get(state.workflow_stage if state else WorkflowStage.REQUIREMENTS, "white")
        try:
            status = self.query_one("#status-bar", Static)
            status.update(
                f"[dim]session:[/dim] {sid}   "
                f"[dim]turn:[/dim] {turns}   "
                f"[dim]stage:[/dim] [{color}]{stage}[/{color}]"
            )
        except Exception:
            pass  # Not mounted yet.

    def _refresh_session_list(self) -> None:
        """Rebuild the session list in the sidebar."""
        container = self.query_one("#session-items-container", Vertical)
        container.remove_children()

        # Include persisted sessions.
        for s in self._agent.list_sessions():
            sid = s.get("session_id", "")
            if sid not in self._sessions:
                self._sessions[sid] = SessionItem(
                    session_id=sid,
                    stage=WorkflowStage(s.get("workflow_stage", "requirements")),
                    turn_count=s.get("turn_count", 0),
                    active=(sid == self._current_session_id),
                )

        # Sort: active first, then by session_id.
        items = sorted(
            self._sessions.values(),
            key=lambda x: (0 if x.active else 1, x.session_id),
        )
        for item in items:
            container.mount(SessionListItem(item))

    def _refresh_stage_stepper(self) -> None:
        try:
            stepper = self.query_one("#stage-stepper", StageStepper)
            stepper.current_stage = self._current_stage
        except Exception:
            pass  # Not mounted yet.

    def _add_user_message(self, text: str) -> None:
        msg = ChatMessage(role="user", content=text)
        self._messages.append(msg)
        self._append_to_log(msg)

    def _add_assistant_message(self, text: str) -> None:
        msg = ChatMessage(role="assistant", content=text)
        self._messages.append(msg)
        self._append_to_log(msg)

    def _add_tool_result(self, tool_name: str, result: dict[str, Any]) -> None:
        msg = ChatMessage(role="tool", content="", tool_name=tool_name, tool_result=result)
        self._messages.append(msg)
        self._append_to_log(msg)
        # Also show in collapsible tool results panel.
        panel = ToolResultPanel(tool_name, result)
        container = self.query_one("#tool-results-container", Vertical)
        container.mount(panel)

    def _append_to_log(self, msg: ChatMessage) -> None:
        log = self.query_one("#message-log", MessageLog)
        log.add_message(msg)
        # Auto-scroll.
        chat_area = self.query_one("#chat-area", VerticalScroll)
        chat_area.scroll_end(animate=False)

    def _clear_chat(self) -> None:
        self._messages.clear()
        try:
            container = self.query_one("#tool-results-container", Vertical)
            container.remove_children()
        except Exception:
            pass  # Not mounted yet.
        try:
            log = self.query_one("#message-log", MessageLog)
            log.clear_messages()
        except Exception:
            pass  # Not mounted yet.

    def _cycle_stage(self) -> None:
        """Cycle to the next valid workflow stage."""
        state = self._current_state
        if state is None:
            return
        idx = ALL_STAGES.index(state.workflow_stage)
        # Get valid next stages.
        from dbk.agent.state import _WORKFLOW_TRANSITIONS
        valid = _WORKFLOW_TRANSITIONS.get(state.workflow_stage, [])
        if not valid:
            return
        # Find the next stage in the list order that is valid.
        for i in range(idx + 1, len(ALL_STAGES)):
            if ALL_STAGES[i] in valid:
                self._advance_to_stage(ALL_STAGES[i])
                return
        # If no forward option, try wrapping.
        for candidate in valid:
            if ALL_STAGES.index(candidate) < idx:
                self._advance_to_stage(candidate)
                return

    def _advance_to_stage(self, target: WorkflowStage) -> None:
        """Move the current session to target stage."""
        if not self._current_session_id:
            return
        try:
            new_state = self._agent.advance_workflow(self._current_session_id, target)
            self._update_session_item(new_state)
            self._refresh_stage_stepper()
            self._refresh_status()
        except ValueError as exc:
            self.notify(f"Stage error: {exc}", severity="error")

    def _get_valid_transitions(self) -> list[WorkflowStage]:
        state = self._current_state
        if state is None:
            return []
        from dbk.agent.state import _WORKFLOW_TRANSITIONS
        return _WORKFLOW_TRANSITIONS.get(state.workflow_stage, [])

    # -------------------------------------------------------------------------
    # User interactions
    # -------------------------------------------------------------------------

    async def _on_send(self) -> None:
        """Handle send button / Enter key in input."""
        input_widget = self.query_one("#input-field", Input)
        text = input_widget.value.strip()
        if not text:
            return
        input_widget.value = ""

        self._add_user_message(text)

        # Disable send while processing.
        send_btn = self.query_one("#send-button", Button)
        send_btn.disabled = True

        try:
            # Run agent in a thread pool so TUI stays responsive.
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                self._agent.process_message,
                text,
                self._current_session_id,
            )

            # Update session after processing.
            self._current_session_id = result.get("session_id", self._current_session_id or "")
            state = self._current_state
            if state:
                self._update_session_item(state)

            # Tool results.
            tool_results = result.get("tool_results", [])
            for tr in tool_results:
                self._add_tool_result(tr.get("tool", "?"), tr)

            # Assistant response content.
            content = result.get("content", "")
            if content:
                self._add_assistant_message(content)

            # If streaming is available, also show the streaming variant on
            # the next message. For now just show the complete response.
        except Exception as exc:
            self._add_assistant_message(f"[red]Error: {exc}[/red]")
        finally:
            send_btn.disabled = False
            self._refresh_status()
            self._refresh_session_list()
            self._refresh_stage_stepper()

    async def _stream_response(self, text: str) -> None:
        """Stream tokens into the last assistant message with typewriter effect."""
        if not text:
            return
        log = self.query_one("#message-log", MessageLog)

        # Make sure we have an assistant message.
        if not self._messages or self._messages[-1].role != "assistant":
            self._add_assistant_message("")
        else:
            # Update last assistant message.
            self._messages[-1].content = ""

        last = self._messages[-1]
        for chunk in text.split():
            last.content += chunk + " "
            log.update_last_message_content(chunk + " ")
            self._refresh_status()
            await asyncio.sleep(self._typewriter_delay)
            chat_area = self.query_one("#chat-area", VerticalScroll)
            chat_area.scroll_end(animate=False)

    # -------------------------------------------------------------------------
    # Actions (bound to keyboard shortcuts + buttons)
    # -------------------------------------------------------------------------

    async def action_quit(self) -> None:
        self.exit()

    def action_clear_chat(self) -> None:
        self._clear_chat()

    def action_cycle_stage(self) -> None:
        self._cycle_stage()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "send-button":
            await self._on_send()
        elif button_id == "btn-new-session":
            self._init_session()
            self._clear_chat()
            self._refresh_session_list()
            self._refresh_status()
            self._refresh_stage_stepper()
        elif button_id == "btn-advance":
            # Advance to the next valid stage.
            valid = self._get_valid_transitions()
            if valid:
                # Prefer the first forward stage.
                state = self._current_state
                if state:
                    from dbk.agent.state import _WORKFLOW_TRANSITIONS
                    all_valid = _WORKFLOW_TRANSITIONS.get(state.workflow_stage, [])
                    candidates = [s for s in all_valid if ALL_STAGES.index(s) > ALL_STAGES.index(state.workflow_stage)]
                    if candidates:
                        self._advance_to_stage(candidates[0])
                    elif all_valid:
                        self._advance_to_stage(all_valid[0])
        elif button_id == "btn-back":
            # Go back to the previous stage (reverse lookup).
            state = self._current_state
            if state:
                from dbk.agent.state import _WORKFLOW_TRANSITIONS
                valid = _WORKFLOW_TRANSITIONS.get(state.workflow_stage, [])
                # Find a stage earlier in the list.
                candidates = [s for s in valid if ALL_STAGES.index(s) < ALL_STAGES.index(state.workflow_stage)]
                if candidates:
                    self._advance_to_stage(max(candidates, key=lambda s: ALL_STAGES.index(s)))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._on_send()

    def on_click(self, event: Click) -> None:
        """Handle click on session list items."""
        # Find if a SessionListItem was clicked.
        try:
            widget = event.style
        except Exception:
            pass
