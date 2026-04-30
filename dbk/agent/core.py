"""Agent core: orchestrates providers, tools, intent, workflow, and sessions."""
from __future__ import annotations

import json
import sys
from typing import Any, Generator

from dbk.agent.intent import IntentRecognizer
from dbk.agent.session import SessionManager
from dbk.agent.session_store import SessionStore
from dbk.agent.state import AgentState, WorkflowStage
from dbk.agent.tools import ToolRegistry
from dbk.agent.workflow import WorkflowStateMachine
from dbk.providers.base import BaseProvider, CompletionMessage, CompletionResponse
from dbk.providers.mock import MockProvider

# Optional plugin support (lazy import to avoid breaking non-plugin installs).
_plugin_registry = None


def _get_plugin_registry():
    global _plugin_registry
    if _plugin_registry is None:
        try:
            from dbk.plugins import get_plugin_registry as _gpr
            _plugin_registry = _gpr()
        except Exception:  # noqa: BLE001
            from dbk.plugins import PluginRegistry
            _plugin_registry = PluginRegistry()
    return _plugin_registry


SYSTEM_PROMPT = """You are a DBK (Database Kernel observability) AI assistant.

You help users manage PostgreSQL kernel observability tasks including:
- Collecting runtime metrics (CPU, memory, disk, connections)
- Querying and analyzing stored metrics
- Diagnosing latency incidents
- Running execution traces (strace, perf)
- Managing collector daemons
- Cleanup and retention operations
- Health checks and configuration validation

Available tools: collect_metrics, query_metrics, health_check, diagnose_incident,
run_trace, cleanup_data, start_collector_daemon, stop_collector_daemon,
daemon_status, validate_config, list_daemons, cleanup_report.

Be concise and actionable. Prioritize correctness and safety (dry-run before destructive ops).
When uncertain, explain the risk and suggest a safer alternative.
"""


class Agent:
    """Main AI Agent class for DBK."""

    def __init__(
        self,
        provider: BaseProvider | None = None,
        session_store: SessionStore | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._provider = provider or MockProvider()
        self._session_store = session_store or SessionStore()
        self._session_manager = SessionManager()
        self._tool_registry = tool_registry or ToolRegistry()
        self._intent_recognizer = IntentRecognizer(provider=self._provider)
        # Discover and apply plugins.
        plugin_reg = _get_plugin_registry()
        if not getattr(plugin_reg, "_loaded", False):
            plugin_reg.discover()
        plugin_reg.apply_tool_hooks(self._tool_registry)
        plugin_reg.apply_agent_init_hooks(self)

    @property
    def provider(self) -> BaseProvider:
        return self._provider

    @property
    def tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    def create_session(self, session_id: str | None = None, goal: str = "") -> AgentState:
        """Create a new agent session."""
        state = self._session_manager.create_session(session_id=session_id, goal=goal)
        self._session_store.save(state)
        return state

    def get_session(self, session_id: str) -> AgentState | None:
        """Get an existing session from store or memory."""
        state = self._session_manager.get_session(session_id)
        if state is not None:
            return state
        return self._session_store.load(session_id)

    def process_message(
        self,
        message: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Process a user message and return a response dict."""
        # Resolve or create session.
        state = self._session_manager.get_session(session_id or "") if session_id else None
        if state is None:
            state = self.create_session(session_id=session_id)

        # Recognize intent.
        intent, params = self._intent_recognizer.recognize(message)
        state = AgentState(
            session_id=state.session_id,
            workflow_stage=state.workflow_stage,
            workflow_goal=state.workflow_goal,
            intent=intent,
            tool_calls=state.tool_calls,
            last_tool_result=state.last_tool_result,
            conversation_history=state.conversation_history,
            metadata=state.metadata,
            created_at=state.created_at,
            turn_count=state.turn_count,
        )

        # Build conversation for LLM.
        system_msg = CompletionMessage(role="system", content=SYSTEM_PROMPT)
        history_msgs = [
            CompletionMessage(role=m["role"], content=m["content"])
            for m in state.conversation_history
        ]
        tool_schemas = self._tool_registry.tool_schemas()
        tool_context = "\n".join(
            f"- {t['name']}: {t['description']}" for t in tool_schemas
        )
        user_with_tools = (
            f"{message}\n\n[Available tools]:\n{tool_context}\n"
            f"[Current workflow stage: {state.workflow_stage.value}]"
        )
        all_msgs = [system_msg] + history_msgs + [CompletionMessage(role="user", content=user_with_tools)]

        # Get LLM response.
        try:
            response = self._provider.chat_with_retry(all_msgs)
        except Exception as exc:  # noqa: BLE001
            return {
                "session_id": state.session_id,
                "content": f"Provider error: {exc}",
                "intent": intent,
                "tool_calls": [],
                "workflow_stage": state.workflow_stage.value,
                "error": str(exc),
            }

        # Parse tool calls from response (simple JSON block detection).
        tool_calls = self._parse_tool_calls(response.content)

        # Execute tools.
        tool_results: list[dict[str, Any]] = []
        for tc in tool_calls:
            tool_name = tc.get("name", "")
            tool = self._tool_registry.get(tool_name)
            if tool:
                result = tool.execute(**tc.get("parameters", {}))
                tool_results.append({"tool": tool_name, **result})
            else:
                tool_results.append({"tool": tool_name, "ok": False, "error": "Unknown tool"})

        # Build output text.
        output_text = self._build_response_text(response.content, tool_results)

        # Update state.
        new_state = state.add_turn(message, output_text)
        new_state = AgentState(
            session_id=new_state.session_id,
            workflow_stage=new_state.workflow_stage,
            workflow_goal=new_state.workflow_goal,
            intent=intent,
            tool_calls=state.tool_calls + [{"intent": intent, "params": params, "results": tool_results}],
            last_tool_result=tool_results[-1] if tool_results else None,
            conversation_history=new_state.conversation_history,
            metadata=new_state.metadata,
            created_at=new_state.created_at,
            updated_at=new_state.updated_at,
            turn_count=new_state.turn_count,
        )
        self._session_manager.update_session(new_state)
        self._session_store.save(new_state)

        return {
            "session_id": new_state.session_id,
            "content": output_text,
            "intent": intent,
            "tool_calls": tool_calls,
            "tool_results": tool_results,
            "workflow_stage": new_state.workflow_stage.value,
            "turn_count": new_state.turn_count,
        }

    def process_stream(
        self,
        message: str,
        session_id: str | None = None,
    ) -> Generator[str, None, dict[str, Any]]:
        """Stream response tokens. Yields tokens, returns metadata dict."""
        state = self._session_manager.get_session(session_id or "") if session_id else None
        if state is None:
            state = self.create_session(session_id=session_id)

        intent, params = self._intent_recognizer.recognize(message)
        system_msg = CompletionMessage(role="system", content=SYSTEM_PROMPT)
        history_msgs = [
            CompletionMessage(role=m["role"], content=m["content"])
            for m in state.conversation_history
        ]
        tool_context = "\n".join(
            f"- {t['name']}: {t['description']}"
            for t in self._tool_registry.tool_schemas()
        )
        user_with_tools = (
            f"{message}\n\n[Available tools]:\n{tool_context}\n"
            f"[Current workflow stage: {state.workflow_stage.value}]"
        )
        all_msgs = [system_msg] + history_msgs + [CompletionMessage(role="user", content=user_with_tools)]

        full_content = ""
        try:
            if self._provider.supports_streaming:
                for token in self._provider.chat_stream(all_msgs):
                    full_content += token
                    yield token
            else:
                response = self._provider.chat_with_retry(all_msgs)
                full_content = response.content
                for word in full_content.split():
                    yield word + " "
        except Exception as exc:  # noqa: BLE001
            yield f"[Provider error: {exc}]"
            full_content = f"[Provider error: {exc}]"

        # Update session after stream completes.
        new_state = state.add_turn(message, full_content)
        self._session_manager.update_session(new_state)
        self._session_store.save(new_state)

        tool_calls = self._parse_tool_calls(full_content)
        return {
            "session_id": new_state.session_id,
            "intent": intent,
            "tool_calls": tool_calls,
            "workflow_stage": new_state.workflow_stage.value,
        }

    def advance_workflow(self, session_id: str, target: WorkflowStage) -> AgentState:
        """Advance session workflow to a new stage."""
        state = self.get_session(session_id)
        if state is None:
            raise KeyError(f"Session not found: {session_id}")
        new_state = state.advance_workflow(target)
        self._session_manager.update_session(new_state)
        self._session_store.save(new_state)
        return new_state

    def list_sessions(self) -> list[dict[str, Any]]:
        """List persisted sessions."""
        return self._session_store.list_sessions()

    def _parse_tool_calls(self, text: str) -> list[dict[str, Any]]:
        """Extract tool call blocks from LLM response text."""
        calls: list[dict[str, Any]] = []
        # Look for JSON code blocks with tool calls.
        import re
        # Match ```json ... ``` blocks
        for match in re.finditer(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL):
            try:
                data = json.loads(match.group(1))
                if isinstance(data, dict) and "name" in data:
                    calls.append(data)
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "name" in item:
                            calls.append(item)
            except json.JSONDecodeError:
                pass
        return calls

    def _build_response_text(
        self,
        llm_content: str,
        tool_results: list[dict[str, Any]],
    ) -> str:
        """Build the final response text combining LLM content and tool results."""
        if not tool_results:
            return llm_content
        lines = [llm_content, "", "--- Tool Results ---"]
        for tr in tool_results:
            tool_name = tr.get("tool", "?")
            if tr.get("ok"):
                result_str = json.dumps(tr.get("result", {}), indent=2)
                lines.append(f"[{tool_name}] OK:")
                lines.append(result_str)
            else:
                lines.append(f"[{tool_name}] ERROR: {tr.get('error', 'unknown')}")
        return "\n".join(lines)

    def info(self) -> dict[str, Any]:
        """Return agent configuration info."""
        plugin_reg = _get_plugin_registry()
        return {
            "provider": self._provider.name,
            "is_mock": self._provider.is_mock,
            "model": getattr(self._provider, "_default_model", None),
            "tool_count": len(self._tool_registry.list_all()),
            "tools": [t.name for t in self._tool_registry.list_all()],
            "plugins": plugin_reg.list_plugins(),
            "plugin_count": plugin_reg.plugin_count,
        }
