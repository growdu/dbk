"""Mock provider for offline/development use."""
from __future__ import annotations

import re
from typing import Any, Generator

from dbk.providers.base import (
    BaseProvider,
    CompletionMessage,
    CompletionResponse,
)


# Intent keywords for mock mode.
_INTENT_PATTERNS: dict[str, list[str]] = {
    "collect_metrics": [
        "collect", "collect metrics", "runtime metrics", "gather metrics",
        "collect runtime", "ingest metrics", "scrape metrics",
    ],
    "query_metrics": [
        "query", "show metrics", "display metrics", "list metrics",
        "latest metrics", "get metrics", "retrieve metrics", "fetch metrics",
    ],
    "health_check": [
        "health", "health check", "status", "collector health", "readiness",
    ],
    "diagnose_incident": [
        "diagnose", "latency", "incident", "slow query", "performance issue",
        "bottleneck", "investigate", "troubleshoot",
    ],
    "run_trace": [
        "trace", "profiling", "profile", "strace", "perf", "stack trace",
        "execution trace", "run trace",
    ],
    "cleanup_data": [
        "cleanup", "clean up", "purge", "delete old", "remove old",
        "retention", "expire data", "garbage collect",
    ],
    "start_daemon": [
        "start daemon", "run daemon", "launch daemon", "start collector",
        "run collector daemon", "start collecting",
    ],
    "stop_daemon": [
        "stop daemon", "stop collector", "halt daemon", "kill daemon",
    ],
    "daemon_status": [
        "daemon status", "is daemon running", "daemon info", "collector status",
    ],
    "validate_config": [
        "validate", "validate config", "check config", "configuration check",
        "verify config", "config validation",
    ],
    "list_daemons": [
        "list daemons", "list collectors", "show daemons", "running daemons",
    ],
    "cleanup_report": [
        "cleanup report", "retention report", "cleanup history", "cleanup summary",
    ],
}

_WORKFLOW_PATTERNS: dict[str, list[str]] = {
    "requirements": ["requirements", "gather requirements", "what do we need"],
    "design": ["design", "architecture", "design doc", "specification", "plan"],
    "implement": ["implement", "code", "write code", "develop", "build"],
    "test": ["test", "testing", "unit test", "integration test", "verify"],
    "runtime": ["runtime", "deploy", "deployment", "rollout", "release"],
    "doc": ["documentation", "docs", "readme", "document", "commentary"],
    "ops": ["ops", "operational", "monitoring", "alerts", "runbook"],
    "done": ["done", "complete", "finished", "wrap up", "summary"],
}


def _best_intent_match(text: str) -> str:
    text_lower = text.lower()
    best = "general"
    best_score = 0
    for intent, keywords in _INTENT_PATTERNS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > best_score:
            best_score = score
            best = intent
    return best if best_score > 0 else "general"


def _best_workflow_match(text: str) -> str:
    text_lower = text.lower()
    for state, keywords in _WORKFLOW_PATTERNS.items():
        if any(kw in text_lower for kw in keywords):
            return state
    return "requirements"


def _build_mock_response(intent: str, workflow_state: str, user_message: str) -> str:
    if intent != "general":
        tool_note = f"[Tool identified: {intent}]"
    else:
        tool_note = "[No specific tool matched]"

    if workflow_state != "requirements":
        workflow_note = f"[Workflow state: {workflow_state}]"
    else:
        workflow_note = "[Workflow state: requirements]"

    suggestions = [
        "I can help you with database kernel observability.",
        "Try commands like: collect metrics, query metrics, health check, diagnose incident,",
        "run trace, cleanup data, or manage collector daemons.",
        "For example: 'dbk collect --source mock --instance pg-main-01'",
        "Or: 'dbk metrics --metric cpu_usage --limit 10'",
    ]

    return (
        f"{tool_note}\n"
        f"{workflow_note}\n\n"
        f"You said: {user_message}\n\n"
        + "\n".join(suggestions)
    )


class MockProvider(BaseProvider):
    """Mock provider that responds without calling any external API."""

    name: str = "mock"
    supports_streaming: bool = True

    @property
    def is_mock(self) -> bool:
        return True

    def chat(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        # Extract the last user message.
        user_text = ""
        for msg in reversed(messages):
            if msg.role == "user":
                user_text = msg.content
                break

        intent = _best_intent_match(user_text)
        workflow = _best_workflow_match(user_text)
        content = _build_mock_response(intent, workflow, user_text)

        return CompletionResponse(
            content=content,
            model="mock/model",
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            finish_reason="stop",
            raw={},
        )

    def chat_stream(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        response = self.chat(messages, model=model, **kwargs)
        for word in response.content.split():
            yield word + " "
