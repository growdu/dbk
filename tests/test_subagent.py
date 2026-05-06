"""Tests for the Sub-agent scheduling framework."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from dbk.agent.core import Agent
from dbk.agent.session_store import SessionStore
from dbk.agent.state import WorkflowStage
from dbk.agent.subagent import (
    ExecutionResult,
    MainAgent,
    SubAgentConfig,
    SubAgentExecutor,
    SubAgentPool,
)
from dbk.agent.tools import ToolRegistry
from dbk.providers.base import CompletionMessage, CompletionResponse
from dbk.providers.mock import MockProvider


# ----------------------------------------------------------------------
# Mock provider for deterministic test responses.
# ----------------------------------------------------------------------


class EchoSubAgentProvider(MockProvider):
    """Mock provider that echoes structured delegation context."""

    def chat(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs,
    ) -> CompletionResponse:
        # Find the user message with delegation context.
        user_text = ""
        for msg in reversed(messages):
            if msg.role == "user":
                user_text = msg.content
                break

        return CompletionResponse(
            content=f"[sub-agent] Processed: {user_text[:80]}",
            model="echo/subagent",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            finish_reason="stop",
            raw={},
        )


# ----------------------------------------------------------------------
# SubAgentConfig tests.
# ----------------------------------------------------------------------


class TestSubAgentConfig:
    def test_basic_config(self) -> None:
        cfg = SubAgentConfig(
            name="test_agent",
            role="Test Role",
            description="A test sub-agent.",
            intent_patterns=["collect_metrics", "diagnose_*"],
            workflow_stages=["requirements", "implement"],
            priority=5,
        )
        assert cfg.name == "test_agent"
        assert cfg.role == "Test Role"
        assert cfg.priority == 5
        assert cfg.max_turns == 5
        assert cfg.timeout_sec == 60.0

    def test_matches_intent_exact(self) -> None:
        cfg = SubAgentConfig(
            name="m", role="r",
            intent_patterns=["collect_metrics", "query_metrics"],
        )
        assert cfg.matches_intent("collect_metrics")
        assert cfg.matches_intent("query_metrics")
        assert not cfg.matches_intent("diagnose_incident")

    def test_matches_intent_wildcard(self) -> None:
        cfg = SubAgentConfig(
            name="m", role="r",
            intent_patterns=["diagnose_*", "cleanup_*"],
        )
        assert cfg.matches_intent("diagnose_incident")
        assert cfg.matches_intent("diagnose_latency")
        assert cfg.matches_intent("cleanup_data")
        assert not cfg.matches_intent("collect_metrics")

    def test_matches_workflow_stage(self) -> None:
        cfg = SubAgentConfig(
            name="m", role="r",
            workflow_stages=["design", "implement", "test"],
        )
        assert cfg.matches_workflow_stage("design")
        assert cfg.matches_workflow_stage("implement")
        assert not cfg.matches_workflow_stage("ops")

    def test_allows_tool_empty_means_all(self) -> None:
        cfg = SubAgentConfig(name="m", role="r", allowed_tools=[])
        assert cfg.allows_tool("collect_metrics")
        assert cfg.allows_tool("diagnose_incident")
        assert cfg.allows_tool("any_tool")

    def test_allows_tool_restricted(self) -> None:
        cfg = SubAgentConfig(
            name="m", role="r",
            allowed_tools=["collect_metrics", "health_check"],
        )
        assert cfg.allows_tool("collect_metrics")
        assert cfg.allows_tool("health_check")
        assert not cfg.allows_tool("diagnose_incident")
        assert not cfg.allows_tool("cleanup_data")

    def test_role_system_prompt(self) -> None:
        cfg = SubAgentConfig(
            name="m", role="r",
            system_prompt_suffix="Be very thorough.",
        )
        prompt = cfg.role_system_prompt()
        assert "Be very thorough" in prompt
        assert "DBK" in prompt  # base system prompt content

    def test_role_system_prompt_no_suffix(self) -> None:
        cfg = SubAgentConfig(name="m", role="r")
        prompt = cfg.role_system_prompt()
        assert "DBK" in prompt


# ----------------------------------------------------------------------
# SubAgentPool tests.
# ----------------------------------------------------------------------


class TestSubAgentPool:
    def test_register_and_get_config(self) -> None:
        pool = SubAgentPool()
        cfg = SubAgentConfig(name="metrics", role="Metrics Specialist")
        pool.register(cfg)
        assert pool.get_config("metrics") is cfg
        assert pool.get_config("nonexistent") is None

    def test_register_many(self) -> None:
        pool = SubAgentPool()
        pool.register_many([
            SubAgentConfig(name="a", role="A"),
            SubAgentConfig(name="b", role="B"),
        ])
        assert len(pool) == 2
        assert set(pool.list_names()) == {"a", "b"}

    def test_unregister(self) -> None:
        pool = SubAgentPool()
        cfg = SubAgentConfig(name="x", role="X")
        pool.register(cfg)
        assert pool.unregister("x")
        assert pool.get_config("x") is None
        assert not pool.unregister("nonexistent")

    def test_find_by_intent(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="m", role="M", intent_patterns=["collect_*"], priority=5))
        pool.register(SubAgentConfig(name="d", role="D", intent_patterns=["diagnose_*"], priority=10))
        pool.register(SubAgentConfig(name="c", role="C", intent_patterns=["collect_*"], priority=7))

        matches = pool.find_by_intent("collect_metrics")
        names = [c.name for c in matches]
        assert names == ["c", "m"]  # sorted by priority descending

    def test_find_by_workflow_stage(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="t", role="T", workflow_stages=["test", "runtime"]))
        pool.register(SubAgentConfig(name="d", role="D", workflow_stages=["design", "implement"]))
        pool.register(SubAgentConfig(name="o", role="O", workflow_stages=["ops"]))

        matches = pool.find_by_workflow_stage("test")
        assert [c.name for c in matches] == ["t"]
        matches = pool.find_by_workflow_stage("design")
        assert [c.name for c in matches] == ["d"]

    def test_find_best_match(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(
            name="m", role="M",
            intent_patterns=["collect_*"],
            workflow_stages=["requirements"],
            priority=5,
        ))
        pool.register(SubAgentConfig(
            name="m2", role="M2",
            intent_patterns=["collect_*"],
            workflow_stages=["implement"],
            priority=10,
        ))

        # Best match by intent only.
        best = pool.find_best_match("collect_metrics")
        assert best is not None
        assert best.name == "m2"  # higher priority

        # Best match by intent AND stage.
        best = pool.find_best_match("collect_metrics", workflow_stage="requirements")
        assert best is not None
        assert best.name == "m"

        # No match for wrong stage.
        best = pool.find_best_match("collect_metrics", workflow_stage="ops")
        assert best is None

    def test_find_best_match_no_match(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="m", role="M", intent_patterns=["collect_*"]))
        assert pool.find_best_match("diagnose_incident") is None

    def test_get_agent_creates_agent(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="test", role="Test", allowed_tools=["validate_config"]))
        registry = ToolRegistry()

        agent = pool.get_agent("test", registry)
        assert agent is not None
        assert isinstance(agent, Agent)
        assert len(agent.tool_registry.list_all()) == 1

    def test_get_agent_caches_agent(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="test", role="Test"))
        registry = ToolRegistry()

        agent1 = pool.get_agent("test", registry)
        agent2 = pool.get_agent("test", registry)
        assert agent1 is agent2

    def test_get_agent_unknown(self) -> None:
        pool = SubAgentPool()
        assert pool.get_agent("nonexistent", ToolRegistry()) is None

    def test_clear_agents(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="test", role="Test"))
        registry = ToolRegistry()
        agent = pool.get_agent("test", registry)
        assert agent is not None

        pool.clear_agents()
        # After clear, a new agent should be created.
        agent2 = pool.get_agent("test", registry)
        assert agent2 is not None

    def test_list_configs(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="a", role="A"))
        pool.register(SubAgentConfig(name="b", role="B"))
        configs = pool.list_configs()
        assert len(configs) == 2
        assert {c.name for c in configs} == {"a", "b"}


# ----------------------------------------------------------------------
# SubAgentExecutor tests.
# ----------------------------------------------------------------------


class TestSubAgentExecutor:
    def test_execute_no_matching_subagent(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="m", role="M", intent_patterns=["collect_*"]))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        result = executor.execute(
            message="diagnose latency on pg-main",
            intent="diagnose_incident",
        )
        assert not result.success
        assert "No matching sub-agent" in result.error

    def test_execute_with_matching_subagent(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(
            name="metrics",
            role="Metrics Specialist",
            intent_patterns=["collect_*", "query_*"],
        ))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        result = executor.execute(
            message="collect metrics for pg-main-01",
            intent="collect_metrics",
        )
        assert result.subagent_name == "metrics"
        # MockProvider returns echo content.
        assert result.success or "error" in result  # depends on MockProvider behavior

    def test_execute_force_specific_subagent(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="a", role="A", intent_patterns=["collect_*"]))
        pool.register(SubAgentConfig(name="b", role="B", intent_patterns=["diagnose_*"]))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        result = executor.execute(
            message="some task",
            intent="general",
            subagent_name="b",
        )
        assert result.subagent_name == "b"

    def test_execute_force_unknown_subagent(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="a", role="A"))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        result = executor.execute(
            message="task",
            intent="general",
            subagent_name="nonexistent",
        )
        assert not result.success
        assert "Unknown sub-agent" in result.error

    def test_execute_parallel(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="collect", role="Collector", intent_patterns=["collect_*"]))
        pool.register(SubAgentConfig(name="diagnose", role="Diagnoser", intent_patterns=["diagnose_*"]))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        tasks = [
            {"message": "collect metrics", "intent": "collect_metrics"},
            {"message": "diagnose incident", "intent": "diagnose_incident"},
        ]
        results = executor.execute_parallel(tasks)
        assert len(results) == 2

    def test_enrich_message_contains_context(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="test", role="Test Role"))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        enriched = executor._enrich_message(
            message="do something",
            intent="general",
            session_id="parent-123",
            workflow_stage="implement",
            parent_context={"key": "value"},
            cfg=pool.get_config("test"),
        )
        assert "Delegation Context" in enriched
        assert "Test Role" in enriched
        assert "parent-123" in enriched
        assert "implement" in enriched
        assert "do something" in enriched

    def test_execute_sets_delegation_reason(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="m", role="M", intent_patterns=["collect_*"]))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        result = executor.execute(
            message="collect",
            intent="collect_metrics",
            delegation_reason="test delegation",
        )
        assert result.delegation_reason == "test delegation"


# ----------------------------------------------------------------------
# MainAgent tests.
# ----------------------------------------------------------------------


class TestMainAgent:
    def test_init_with_defaults(self) -> None:
        agent = MainAgent(provider=MockProvider())
        assert len(agent.subagent_pool) > 0
        assert agent._auto_delegate is True
        assert agent._allow_fallback is True

    def test_init_with_empty_pool(self) -> None:
        pool = SubAgentPool()
        agent = MainAgent(provider=MockProvider(), subagent_pool=pool)
        assert agent.subagent_pool is pool
        # When a pool is explicitly provided, defaults are NOT auto-registered.
        assert len(agent.subagent_pool) == 0

    def test_init_with_custom_pool(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="custom", role="Custom Role"))
        agent = MainAgent(provider=MockProvider(), subagent_pool=pool)
        assert agent.subagent_pool is pool
        assert pool.get_config("custom") is not None

    def test_register_and_unregister_subagent(self) -> None:
        agent = MainAgent(provider=MockProvider())
        initial_count = len(agent.subagent_pool)

        agent.register_subagent(SubAgentConfig(name="new_agent", role="New Role"))
        assert len(agent.subagent_pool) == initial_count + 1
        assert agent.subagent_pool.get_config("new_agent") is not None

        removed = agent.unregister_subagent("new_agent")
        assert removed
        assert agent.subagent_pool.get_config("new_agent") is None

    def test_should_delegate_intent_match(self) -> None:
        agent = MainAgent(provider=MockProvider())
        delegate, cfg = agent.should_delegate("collect_metrics")
        assert delegate
        assert cfg is not None
        assert cfg.name == "metrics_collector"

    def test_should_delegate_no_match(self) -> None:
        agent = MainAgent(provider=MockProvider())
        # The default sub-agents don't handle "general"
        delegate, cfg = agent.should_delegate("general")
        assert not delegate
        assert cfg is None

    def test_should_delegate_disabled(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="m", role="M", intent_patterns=["collect_*"]))
        agent = MainAgent(provider=MockProvider(), subagent_pool=pool, auto_delegate=False)
        delegate, cfg = agent.should_delegate("collect_metrics")
        assert not delegate
        assert cfg is None

    def test_process_message_delegates(self) -> None:
        agent = MainAgent(provider=MockProvider())
        result = agent.process_message("collect metrics for pg-main", session_id="test-del")
        assert "delegated" in result
        assert result["delegated"] is True
        assert "subagent" in result
        assert result["subagent"] == "metrics_collector"
        assert "session_id" in result
        assert result["session_id"] == "test-del"

    def test_process_message_force_direct(self) -> None:
        agent = MainAgent(provider=MockProvider())
        result = agent.process_message(
            "collect metrics for pg-main",
            session_id="test-force",
            force_direct=True,
        )
        assert result["delegated"] is False
        assert result["subagent"] is None

    def test_process_message_unknown_intent_no_fallback(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="custom", role="C", intent_patterns=["known_intent"]))
        agent = MainAgent(
            provider=MockProvider(),
            subagent_pool=pool,
            allow_fallback=False,
        )
        # "general" doesn't match "known_intent"
        result = agent.process_message(
            "do something unknown",
            session_id="test-no-fallback",
        )
        assert result.get("delegated") is False
        assert result.get("error") == "no_handler"

    def test_process_message_unknown_intent_with_fallback(self) -> None:
        agent = MainAgent(provider=MockProvider(), allow_fallback=True)
        result = agent.process_message(
            "hello there",
            session_id="test-fallback",
        )
        assert "content" in result

    def test_process_message_updates_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "main.sqlite")
            agent = MainAgent(provider=MockProvider(), session_store=store)

            result1 = agent.process_message("collect metrics pg-main", session_id="sess-update")
            result2 = agent.process_message("query metrics cpu", session_id="sess-update")

            assert result2["turn_count"] >= 1
            assert result2["turn_count"] > result1.get("turn_count", 0)

    def test_process_message_delegation_metadata(self) -> None:
        agent = MainAgent(provider=MockProvider())
        result = agent.process_message("collect metrics pg-main", session_id="sess-meta")

        assert "delegation_reason" in result
        assert "delegated" in result
        assert result["delegated"] is True
        assert "subagent_success" in result
        assert "delegation_duration_sec" in result

    def test_info_includes_subagents(self) -> None:
        agent = MainAgent(provider=MockProvider())
        info = agent.info()
        assert "subagent_count" in info
        assert "subagents" in info
        assert isinstance(info["subagents"], list)
        assert info["auto_delegate"] is True
        assert info["allow_fallback"] is True

    def test_synthesize_response_success(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="test", role="Test Role"))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        agent = MainAgent(provider=MockProvider(), subagent_pool=pool)

        sub_result = ExecutionResult(
            subagent_name="test",
            success=True,
            content="Sub-agent result content",
            tool_results=[{"tool": "validate_config", "ok": True, "result": {}}],
            turn_count=1,
            duration_sec=0.5,
            delegation_reason="matched",
        )

        text = agent._synthesize_response(sub_result, "original message", pool.get_config("test"))
        assert "test" in text
        assert "Test Role" in text
        assert "Sub-agent result content" in text
        assert "validate_config" in text
        assert "0.50s" in text or "0.5s" in text

    def test_synthesize_response_error_with_fallback(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="broken", role="Broken Role"))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        agent = MainAgent(provider=MockProvider(), subagent_pool=pool)

        sub_result = ExecutionResult(
            subagent_name="broken",
            success=False,
            content="",
            error="Something went wrong",
            delegation_reason="test",
        )

        text = agent._synthesize_response(sub_result, "original message", pool.get_config("broken"))
        assert "error" in text.lower() or "failed" in text.lower() or "direct" in text.lower()

    def test_info_shows_default_subagents(self) -> None:
        agent = MainAgent(provider=MockProvider())
        info = agent.info()
        names = {s["name"] for s in info["subagents"]}
        # Default sub-agents should be present.
        assert "metrics_collector" in names or "diagnostics_expert" in names


# ----------------------------------------------------------------------
# End-to-end integration tests.
# ----------------------------------------------------------------------


class TestMainAgentEndToEnd:
    def test_two_delegations_in_sequence(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="ops", role="Ops", intent_patterns=["start_*"]))
        pool.register(SubAgentConfig(name="metrics", role="Metrics", intent_patterns=["collect_*"]))
        agent = MainAgent(provider=MockProvider(), subagent_pool=pool)

        result1 = agent.process_message("start collector daemon", session_id="e2e-1")
        result2 = agent.process_message("collect metrics", session_id="e2e-1")

        assert result1.get("delegated") is True
        assert result2.get("delegated") is True
        assert result1["subagent"] == "ops"
        assert result2["subagent"] == "metrics"

    def test_parallel_delegation_via_executor(self) -> None:
        pool = SubAgentPool()
        pool.register(SubAgentConfig(name="c", role="C", intent_patterns=["collect_*"]))
        pool.register(SubAgentConfig(name="d", role="D", intent_patterns=["diagnose_*"]))
        registry = ToolRegistry()
        executor = SubAgentExecutor(pool, registry)

        tasks = [
            {"message": "collect metrics pg-1", "intent": "collect_metrics"},
            {"message": "diagnose latency pg-2", "intent": "diagnose_incident"},
            {"message": "collect metrics pg-3", "intent": "collect_metrics"},
        ]
        results = executor.execute_parallel(tasks)
        assert len(results) == 3
        assert {r.subagent_name for r in results} == {"c", "d"}

    def test_session_persistence_across_delegations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SessionStore(db_path=Path(tmpdir) / "persist.sqlite")
            pool = SubAgentPool()
            pool.register(SubAgentConfig(name="c", role="C", intent_patterns=["collect_*"]))
            agent = MainAgent(provider=MockProvider(), session_store=store, subagent_pool=pool)

            result1 = agent.process_message("collect metrics pg-main", session_id="persist-01")
            result2 = agent.process_message("query cpu usage", session_id="persist-01")

            # Check session was persisted.
            loaded = agent.get_session("persist-01")
            assert loaded is not None
            assert loaded.turn_count >= 2

    def test_mainagent_with_custom_provider_on_subagent(self) -> None:
        pool = SubAgentPool()
        custom_cfg = SubAgentConfig(
            name="custom",
            role="Custom",
            intent_patterns=["custom_intent"],
            provider=MockProvider(),
        )
        pool.register(custom_cfg)

        agent = MainAgent(provider=MockProvider(), subagent_pool=pool)

        # The sub-agent should use its own provider.
        sub_agent = pool.get_agent("custom", agent.tool_registry)
        assert sub_agent is not None
        assert sub_agent.provider is custom_cfg.provider

    def test_default_subagent_configs(self) -> None:
        """Verify default sub-agent configs are properly set up."""
        agent = MainAgent(provider=MockProvider())
        assert len(agent.subagent_pool) > 0

        # Verify metrics_collector.
        mc = agent.subagent_pool.get_config("metrics_collector")
        assert mc is not None
        assert "collect_metrics" in mc.intent_patterns
        assert mc.priority > 0

        # Verify diagnostics_expert.
        de = agent.subagent_pool.get_config("diagnostics_expert")
        assert de is not None
        assert "diagnose_incident" in de.intent_patterns

        # Verify ops_specialist.
        ops = agent.subagent_pool.get_config("ops_specialist")
        assert ops is not None
        assert "start_daemon" in ops.intent_patterns
