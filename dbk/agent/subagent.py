"""Sub-agent scheduling framework for hierarchical multi-agent orchestration.

Classes:
    SubAgentConfig: Configuration for a sub-agent (role, tools, limits).
    SubAgentPool: Manages a registry of SubAgentConfig instances.
    SubAgentExecutor: Executes tasks by routing to the appropriate sub-agent.
    MainAgent: Extends Agent with automatic task delegation to sub-agents.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from dbk.agent.core import Agent, AgentState, SYSTEM_PROMPT
from dbk.agent.intent import IntentRecognizer
from dbk.agent.session import SessionManager
from dbk.agent.session_store import SessionStore
from dbk.agent.state import WorkflowStage
from dbk.agent.tools import Tool, ToolRegistry
from dbk.providers.base import BaseProvider, CompletionMessage, CompletionResponse
from dbk.providers.mock import MockProvider


# ----------------------------------------------------------------------
# Sub-agent configuration
# ----------------------------------------------------------------------


@dataclass(slots=True)
class SubAgentConfig:
    """Configuration for a sub-agent in the delegation hierarchy."""

    name: str
    role: str
    description: str = ""
    # Tools this sub-agent is allowed to use (names). Empty = all tools.
    allowed_tools: list[str] = field(default_factory=list)
    # Intent patterns this sub-agent handles (e.g. "collect_metrics", "diagnose_*")
    intent_patterns: list[str] = field(default_factory=list)
    # Workflow stages this sub-agent handles
    workflow_stages: list[str] = field(default_factory=list)
    # Max LLM turns per task before forcing result return
    max_turns: int = 5
    # Timeout in seconds for a single execution
    timeout_sec: float = 60.0
    # Priority (higher = selected first when multiple match)
    priority: int = 0
    # Custom provider override for this sub-agent
    provider: BaseProvider | None = None
    # Extra system prompt suffix for this sub-agent's role
    system_prompt_suffix: str = ""

    def matches_intent(self, intent: str) -> bool:
        """Check if this sub-agent handles the given intent."""
        if not self.intent_patterns:
            return False
        for pattern in self.intent_patterns:
            if pattern.endswith("*"):
                prefix = pattern[:-1]
                if intent.startswith(prefix):
                    return True
            elif intent == pattern:
                return True
        return False

    def matches_workflow_stage(self, stage: str) -> bool:
        """Check if this sub-agent handles the given workflow stage."""
        if not self.workflow_stages:
            return False
        return stage in self.workflow_stages

    def allows_tool(self, tool_name: str) -> bool:
        """Check if this sub-agent is allowed to use a tool."""
        if not self.allowed_tools:
            return True  # Empty means all tools allowed
        return tool_name in self.allowed_tools

    def tool_schemas_filtered(self, registry: ToolRegistry) -> list[dict[str, Any]]:
        """Return tool schemas filtered by allowed_tools."""
        all_schemas = registry.tool_schemas()
        if not self.allowed_tools:
            return all_schemas
        return [s for s in all_schemas if s["name"] in self.allowed_tools]

    def role_system_prompt(self) -> str:
        """Return the system prompt for this sub-agent's role."""
        base = SYSTEM_PROMPT
        if self.system_prompt_suffix:
            return f"{base}\n\n{self.system_prompt_suffix}"
        return base


# ----------------------------------------------------------------------
# Sub-agent pool
# ----------------------------------------------------------------------


class SubAgentPool:
    """Registry and factory for sub-agents.

    Manages a collection of SubAgentConfig instances and provides
    lookup by name, intent, or workflow stage.
    """

    def __init__(self) -> None:
        self._configs: dict[str, SubAgentConfig] = {}
        self._agents: dict[str, Agent] = {}  # name -> live Agent instance

    def register(self, config: SubAgentConfig) -> None:
        """Register a sub-agent configuration."""
        self._configs[config.name] = config

    def register_many(self, configs: list[SubAgentConfig]) -> None:
        """Register multiple sub-agent configurations."""
        for cfg in configs:
            self.register(cfg)

    def unregister(self, name: str) -> bool:
        """Remove a sub-agent configuration and stop its agent."""
        if name in self._agents:
            self._agents[name]  # keep reference during cleanup
            del self._agents[name]
        return self._configs.pop(name, None) is not None

    def get_config(self, name: str) -> SubAgentConfig | None:
        """Get config for a named sub-agent."""
        return self._configs.get(name)

    def get_agent(self, name: str, registry: ToolRegistry) -> Agent | None:
        """Get or create a live Agent instance for a sub-agent.

        The agent is configured with the sub-agent's provider (or MockProvider),
        restricted tool registry, and role-appropriate system prompt.
        """
        if name not in self._configs:
            return None

        if name not in self._agents:
            cfg = self._configs[name]
            provider = cfg.provider or MockProvider()
            filtered_registry = self._create_filtered_registry(cfg, registry)
            self._agents[name] = Agent(
                provider=provider,
                session_store=SessionStore(),
                tool_registry=filtered_registry,
            )

        return self._agents.get(name)

    def _create_filtered_registry(
        self, cfg: SubAgentConfig, base_registry: ToolRegistry
    ) -> ToolRegistry:
        """Create a tool registry filtered to the sub-agent's allowed tools.

        Starts from an empty registry and only adds permitted tools,
        avoiding the auto-registration of defaults in ToolRegistry().
        """
        filtered = ToolRegistry.__new__(ToolRegistry)
        filtered._tools = {}
        for tool in base_registry.list_all():
            if cfg.allows_tool(tool.name):
                filtered._tools[tool.name] = tool
        return filtered

    def list_configs(self) -> list[SubAgentConfig]:
        """List all registered sub-agent configs."""
        return list(self._configs.values())

    def list_names(self) -> list[str]:
        """List all registered sub-agent names."""
        return list(self._configs.keys())

    def find_by_intent(self, intent: str) -> list[SubAgentConfig]:
        """Find all sub-agents that handle the given intent, sorted by priority."""
        matches = [cfg for cfg in self._configs.values() if cfg.matches_intent(intent)]
        matches.sort(key=lambda c: c.priority, reverse=True)
        return matches

    def find_by_workflow_stage(self, stage: str) -> list[SubAgentConfig]:
        """Find all sub-agents that handle the given workflow stage."""
        return [cfg for cfg in self._configs.values() if cfg.matches_workflow_stage(stage)]

    def find_best_match(
        self, intent: str, workflow_stage: str | None = None
    ) -> SubAgentConfig | None:
        """Find the best-matching sub-agent for an intent (and optional stage).

        Returns the highest-priority match for the intent. If workflow_stage is
        provided and a candidate has explicit stage restrictions, those are enforced.
        Candidates with no stage restrictions are treated as catch-alls and always match.
        """
        candidates = self.find_by_intent(intent)
        if workflow_stage:
            # Only filter candidates that have explicit stage restrictions.
            candidates = [
                c for c in candidates
                if not c.workflow_stages or c.matches_workflow_stage(workflow_stage)
            ]
        return candidates[0] if candidates else None

    def clear_agents(self) -> None:
        """Remove all live agent instances (configs remain registered)."""
        self._agents.clear()

    def __len__(self) -> int:
        return len(self._configs)


# ----------------------------------------------------------------------
# Sub-agent executor
# ----------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """Result of a sub-agent execution."""

    subagent_name: str
    success: bool
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    intent: str = "general"
    error: str | None = None
    duration_sec: float = 0.0
    turn_count: int = 0
    delegation_reason: str = ""


class SubAgentExecutor:
    """Executes tasks by routing to the appropriate sub-agent from a pool.

    Responsibilities:
      - Match incoming tasks to sub-agents based on intent and workflow stage.
      - Create a sub-agent session, inject delegation context, and execute.
      - Enforce timeouts and turn limits per sub-agent config.
      - Return aggregated ExecutionResult.
    """

    def __init__(
        self,
        pool: SubAgentPool,
        base_tool_registry: ToolRegistry | None = None,
        default_provider: BaseProvider | None = None,
    ) -> None:
        self._pool = pool
        self._base_registry = base_tool_registry or ToolRegistry()
        self._default_provider = default_provider or MockProvider()
        self._sub_sessions: dict[str, str] = {}  # execution_id -> sub-agent session_id

    @property
    def pool(self) -> SubAgentPool:
        return self._pool

    def execute(
        self,
        message: str,
        intent: str,
        session_id: str | None = None,
        workflow_stage: str | None = None,
        parent_context: dict[str, Any] | None = None,
        delegation_reason: str = "",
        subagent_name: str | None = None,
    ) -> ExecutionResult:
        """Execute a task by delegating to the best-matching sub-agent.

        Args:
            message: User message or task description to route.
            intent: Recognized intent for routing.
            session_id: Optional parent session ID for context.
            workflow_stage: Current workflow stage.
            parent_context: Additional context from the main agent.
            delegation_reason: Human-readable reason for delegation.
            subagent_name: Force routing to a specific sub-agent (bypasses matching).

        Returns:
            ExecutionResult with the sub-agent's response.
        """
        start_time = time.time()

        # Resolve sub-agent.
        if subagent_name:
            cfg = self._pool.get_config(subagent_name)
            if cfg is None:
                return ExecutionResult(
                    subagent_name=subagent_name,
                    success=False,
                    content="",
                    error=f"Unknown sub-agent: {subagent_name}",
                    duration_sec=time.time() - start_time,
                    delegation_reason=delegation_reason,
                )
        else:
            cfg = self._pool.find_best_match(intent, workflow_stage)
            if cfg is None:
                return ExecutionResult(
                    subagent_name="",
                    success=False,
                    content="",
                    error="No matching sub-agent found for intent",
                    intent=intent,
                    duration_sec=time.time() - start_time,
                    delegation_reason=delegation_reason,
                )

        # Build sub-agent session.
        execution_id = str(uuid.uuid4())
        agent = self._pool.get_agent(cfg.name, self._base_registry)
        if agent is None:
            return ExecutionResult(
                subagent_name=cfg.name,
                success=False,
                content="",
                error=f"Failed to create agent for sub-agent: {cfg.name}",
                intent=intent,
                duration_sec=time.time() - start_time,
                delegation_reason=delegation_reason,
            )

        sub_session_id = f"sub-{cfg.name}-{execution_id[:8]}"
        self._sub_sessions[execution_id] = sub_session_id

        # Build enriched message with delegation context.
        enriched_message = self._enrich_message(
            message=message,
            intent=intent,
            session_id=session_id,
            workflow_stage=workflow_stage,
            parent_context=parent_context,
            cfg=cfg,
        )

        # Execute with turn limit.
        try:
            result = agent.process_message(
                message=enriched_message,
                session_id=sub_session_id,
            )

            duration = time.time() - start_time
            if duration > cfg.timeout_sec:
                return ExecutionResult(
                    subagent_name=cfg.name,
                    success=False,
                    content=result.get("content", ""),
                    tool_calls=result.get("tool_calls", []),
                    tool_results=result.get("tool_results", []),
                    intent=result.get("intent", intent),
                    error=f"Execution timed out after {duration:.1f}s (limit: {cfg.timeout_sec}s)",
                    duration_sec=duration,
                    turn_count=result.get("turn_count", 0),
                    delegation_reason=delegation_reason,
                )

            return ExecutionResult(
                subagent_name=cfg.name,
                success=True,
                content=result.get("content", ""),
                tool_calls=result.get("tool_calls", []),
                tool_results=result.get("tool_results", []),
                intent=result.get("intent", intent),
                duration_sec=duration,
                turn_count=result.get("turn_count", 0),
                delegation_reason=delegation_reason or f"Matched intent pattern: {intent}",
            )

        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(
                subagent_name=cfg.name,
                success=False,
                content="",
                intent=intent,
                error=f"Sub-agent execution error: {exc}",
                duration_sec=time.time() - start_time,
                delegation_reason=delegation_reason,
            )

    def execute_parallel(
        self,
        tasks: list[dict[str, Any]],
        max_concurrent: int = 4,
    ) -> list[ExecutionResult]:
        """Execute multiple tasks in parallel.

        Args:
            tasks: List of dicts with keys: message, intent, session_id, etc.
            max_concurrent: Maximum number of concurrent sub-agent executions.

        Returns:
            List of ExecutionResult in the same order as tasks.
        """
        results: list[ExecutionResult] = []
        for task in tasks:
            result = self.execute(
                message=task.get("message", ""),
                intent=task.get("intent", "general"),
                session_id=task.get("session_id"),
                workflow_stage=task.get("workflow_stage"),
                parent_context=task.get("parent_context"),
                delegation_reason=task.get("delegation_reason", ""),
                subagent_name=task.get("subagent_name"),
            )
            results.append(result)
        return results

    def _enrich_message(
        self,
        message: str,
        intent: str,
        session_id: str | None,
        workflow_stage: str | None,
        parent_context: dict[str, Any] | None,
        cfg: SubAgentConfig,
    ) -> str:
        """Enrich the user message with delegation context for the sub-agent."""
        lines = [
            f"[Delegation Context]",
            f"You are acting as: {cfg.role}",
            f"Original user intent: {intent}",
            f"Task: {message}",
        ]
        if session_id:
            lines.append(f"Parent session: {session_id}")
        if workflow_stage:
            lines.append(f"Current workflow stage: {workflow_stage}")
        if parent_context:
            ctx_str = json.dumps(parent_context, indent=2, default=str)
            lines.append(f"Additional context:\n{ctx_str}")
        lines.append(f"[/Delegation Context]\n")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Main Agent with auto-delegation
# ----------------------------------------------------------------------


class MainAgent(Agent):
    """Extends Agent with automatic sub-agent task delegation.

    The MainAgent analyzes each incoming message and decides whether to:
      1. Handle it directly (no matching sub-agent).
      2. Delegate to a specialized sub-agent (matches intent/stage patterns).

    Sub-agent results are synthesized back into a unified response.
    """

    # Default built-in sub-agent configurations.
    DEFAULT_SUBAGENTS: list[SubAgentConfig] = [
        SubAgentConfig(
            name="metrics_collector",
            role="Metrics Collector Specialist",
            description="Specializes in collecting and ingesting runtime metrics from PostgreSQL.",
            allowed_tools=["collect_metrics", "query_metrics", "health_check"],
            intent_patterns=["collect_metrics", "query_metrics", "health_check"],
            workflow_stages=["requirements", "implement"],
            priority=10,
            system_prompt_suffix=(
                "You are a metrics collection specialist. Focus on accurate, timely metric "
                "ingestion. Always verify data freshness before reporting results."
            ),
        ),
        SubAgentConfig(
            name="diagnostics_expert",
            role="Diagnostics Expert",
            description="Specializes in diagnosing performance incidents and latency issues.",
            allowed_tools=["diagnose_incident", "run_trace", "query_metrics", "health_check"],
            intent_patterns=["diagnose_incident", "run_trace"],
            workflow_stages=["design", "implement", "test"],
            priority=15,
            system_prompt_suffix=(
                "You are a diagnostics expert. Be systematic: identify symptoms, form "
                "hypotheses, gather evidence, and draw conclusions with confidence levels."
            ),
        ),
        SubAgentConfig(
            name="ops_specialist",
            role="Operations Specialist",
            description="Specializes in daemon management, cleanup, and operational tasks.",
            allowed_tools=[
                "start_collector_daemon",
                "stop_collector_daemon",
                "daemon_status",
                "list_daemons",
                "cleanup_data",
                "cleanup_report",
                "validate_config",
            ],
            intent_patterns=[
                "start_daemon",
                "stop_daemon",
                "daemon_status",
                "list_daemons",
                "cleanup_data",
                "cleanup_report",
                "validate_config",
            ],
            workflow_stages=["runtime", "ops"],
            priority=10,
            system_prompt_suffix=(
                "You are an operations specialist. Prioritize safety (dry-run first), "
                "reliability, and clear operational status reporting."
            ),
        ),
    ]

    def __init__(
        self,
        provider: BaseProvider | None = None,
        session_store: SessionStore | None = None,
        tool_registry: ToolRegistry | None = None,
        subagent_pool: SubAgentPool | None = None,
        auto_delegate: bool = True,
        allow_fallback: bool = True,
    ) -> None:
        """Initialize MainAgent.

        Args:
            provider: LLM provider for the main agent.
            session_store: Session persistence store.
            tool_registry: Base tool registry (shared with sub-agents).
            subagent_pool: Pre-configured pool. If None, uses DEFAULT_SUBAGENTS.
            auto_delegate: If True, automatically route tasks to matching sub-agents.
            allow_fallback: If True and no sub-agent matches, handle task directly.
        """
        super().__init__(
            provider=provider,
            session_store=session_store,
            tool_registry=tool_registry,
        )
        self._subagent_pool = subagent_pool if subagent_pool is not None else SubAgentPool()
        self._subagent_executor = SubAgentExecutor(
            pool=self._subagent_pool,
            base_tool_registry=self._tool_registry,
            default_provider=provider,
        )
        self._auto_delegate = auto_delegate
        self._allow_fallback = allow_fallback

        # Register default sub-agents if pool was not pre-configured.
        # Use an empty sentinel to distinguish "no pool passed" from "empty pool passed".
        if subagent_pool is None and len(self._subagent_pool) == 0:
            self._subagent_pool.register_many(self.DEFAULT_SUBAGENTS)

    @property
    def subagent_pool(self) -> SubAgentPool:
        """The sub-agent pool used by this MainAgent."""
        return self._subagent_pool

    @property
    def subagent_executor(self) -> SubAgentExecutor:
        """The executor for sub-agent tasks."""
        return self._subagent_executor

    def register_subagent(self, config: SubAgentConfig) -> None:
        """Register a sub-agent configuration."""
        self._subagent_pool.register(config)

    def unregister_subagent(self, name: str) -> bool:
        """Unregister a sub-agent by name."""
        return self._subagent_pool.unregister(name)

    def should_delegate(self, intent: str, workflow_stage: str | None = None) -> tuple[bool, SubAgentConfig | None]:
        """Determine whether a task should be delegated.

        Args:
            intent: Recognized intent from the message.
            workflow_stage: Current workflow stage.

        Returns:
            (should_delegate, matched_config)
        """
        if not self._auto_delegate:
            return False, None
        cfg = self._subagent_pool.find_best_match(intent, workflow_stage)
        return cfg is not None, cfg

    def process_message(
        self,
        message: str,
        session_id: str | None = None,
        force_direct: bool = False,
    ) -> dict[str, Any]:
        """Process a message with optional auto-delegation.

        Args:
            message: User message.
            session_id: Session ID for conversation continuity.
            force_direct: If True, skip delegation even if a sub-agent matches.

        Returns:
            Response dict with additional sub-agent metadata if delegation occurred.
        """
        # Resolve or create session.
        state = self._session_manager.get_session(session_id or "") if session_id else None
        if state is None:
            state = self.create_session(session_id=session_id)

        # Recognize intent.
        intent, params = self._intent_recognizer.recognize(message)
        current_stage = state.workflow_stage.value

        # Check if we should delegate.
        delegate, matched_cfg = self.should_delegate(intent, current_stage)

        # Build parent context for delegation.
        parent_context = {
            "session_id": state.session_id,
            "workflow_stage": current_stage,
            "intent": intent,
            "params": params,
            "turn_count": state.turn_count,
            "conversation_turns": len(state.conversation_history),
        }

        # Route to sub-agent or handle directly.
        if delegate and not force_direct and matched_cfg:
            exec_result = self._subagent_executor.execute(
                message=message,
                intent=intent,
                session_id=state.session_id,
                workflow_stage=current_stage,
                parent_context=parent_context,
                delegation_reason=f"Intent '{intent}' matched sub-agent '{matched_cfg.name}' (priority={matched_cfg.priority})",
            )

            output_text = self._synthesize_response(
                subagent_result=exec_result,
                original_message=message,
                matched_cfg=matched_cfg,
            )

            # Update session with delegation record.
            new_state = state.add_turn(message, output_text)
            tool_calls_entry = {
                "intent": intent,
                "params": params,
                "results": exec_result.tool_results,
                "delegated_to": exec_result.subagent_name,
                "delegation_success": exec_result.success,
            }
            new_state = AgentState(
                session_id=new_state.session_id,
                workflow_stage=new_state.workflow_stage,
                workflow_goal=new_state.workflow_goal,
                intent=intent,
                tool_calls=state.tool_calls + [tool_calls_entry],
                last_tool_result=exec_result.tool_results[-1] if exec_result.tool_results else None,
                conversation_history=new_state.conversation_history,
                metadata={
                    **new_state.metadata,
                    "last_delegation": {
                        "subagent": exec_result.subagent_name,
                        "success": exec_result.success,
                        "duration_sec": exec_result.duration_sec,
                        "reason": exec_result.delegation_reason,
                    },
                },
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
                "tool_calls": exec_result.tool_calls,
                "tool_results": exec_result.tool_results,
                "workflow_stage": new_state.workflow_stage.value,
                "turn_count": new_state.turn_count,
                "delegated": True,
                "subagent": exec_result.subagent_name,
                "subagent_success": exec_result.success,
                "subagent_error": exec_result.error,
                "delegation_reason": exec_result.delegation_reason,
                "delegation_duration_sec": exec_result.duration_sec,
            }

        # Direct handling (no matching sub-agent or force_direct).
        if self._allow_fallback or force_direct:
            result = super().process_message(message, session_id=state.session_id)
            result["delegated"] = False
            result["subagent"] = None
            result["delegation_reason"] = None
            return result

        # No delegation, fallback disabled.
        return {
            "session_id": state.session_id,
            "content": f"No sub-agent available to handle intent '{intent}' and direct handling is disabled.",
            "intent": intent,
            "tool_calls": [],
            "workflow_stage": current_stage,
            "turn_count": state.turn_count,
            "delegated": False,
            "subagent": None,
            "error": "no_handler",
        }

    def _synthesize_response(
        self,
        subagent_result: ExecutionResult,
        original_message: str,
        matched_cfg: SubAgentConfig,
    ) -> str:
        """Synthesize a unified response from the sub-agent result.

        Adds a header noting delegation and delegates back to the main agent
        for a final synthesis if needed.
        """
        lines = [
            f"[Delegated to **{matched_cfg.name}** ({matched_cfg.role})]",
            "",
        ]

        if not subagent_result.success and subagent_result.error:
            lines.extend([
                f"Sub-agent encountered an error: {subagent_result.error}",
                "The main agent will handle this task directly.",
                "",
            ])
            # Fall back to direct handling for the error case.
            try:
                direct_result = super().process_message(original_message)
                lines.append(direct_result.get("content", ""))
            except Exception:  # noqa: BLE001
                lines.append("(Direct fallback also failed. Please retry.)")
        else:
            lines.append(subagent_result.content)

            if subagent_result.tool_results:
                lines.append("")
                lines.append("--- Sub-agent Tool Results ---")
                for tr in subagent_result.tool_results:
                    tool_name = tr.get("tool", "?")
                    if tr.get("ok"):
                        result_str = json.dumps(tr.get("result", {}), indent=2, default=str)
                        lines.append(f"[{tool_name}] OK:")
                        lines.append(result_str)
                    else:
                        lines.append(f"[{tool_name}] ERROR: {tr.get('error', 'unknown')}")

            lines.append("")
            lines.append(
                f"*Delegated to {matched_cfg.name} | {subagent_result.duration_sec:.2f}s | "
                f"{subagent_result.turn_count} turns*"
            )

        return "\n".join(lines)

    def info(self) -> dict[str, Any]:
        """Return agent info including sub-agent registry."""
        base_info = super().info()
        return {
            **base_info,
            "subagent_count": len(self._subagent_pool),
            "subagents": [
                {
                    "name": cfg.name,
                    "role": cfg.role,
                    "intent_patterns": cfg.intent_patterns,
                    "workflow_stages": cfg.workflow_stages,
                    "priority": cfg.priority,
                    "allowed_tools": cfg.allowed_tools,
                }
                for cfg in self._subagent_pool.list_configs()
            ],
            "auto_delegate": self._auto_delegate,
            "allow_fallback": self._allow_fallback,
        }
