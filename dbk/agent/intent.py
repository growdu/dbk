"""Intent recognition with keyword + LLM hybrid detection."""
from __future__ import annotations

import re
from typing import Any

from dbk.providers.base import BaseProvider, CompletionMessage, CompletionResponse


# High-confidence keyword patterns for fast intent detection.
_KEYWORD_INTENTS: dict[str, list[str]] = {
    "collect_metrics": [
        "collect metrics", "collect runtime", "gather metrics",
        "scrape metrics", "ingest metrics", "collect now",
        "start collecting",
    ],
    "query_metrics": [
        "query metrics", "show metrics", "display metrics",
        "latest metrics", "get metric", "cpu usage",
        "memory usage", "active connections",
    ],
    "health_check": [
        "health", "health check", "collector health", "is it running",
        "readiness", "is healthy",
    ],
    "diagnose_incident": [
        "diagnose", "latency", "slow", "incident", "bottleneck",
        "investigate", "troubleshoot", "performance issue", "why is",
        "high cpu", "high memory", "query is slow",
    ],
    "run_trace": [
        "trace", "profiling", "profile", "strace", "perf",
        "stack trace", "execution trace",
    ],
    "cleanup_data": [
        "cleanup", "clean up", "purge", "delete old", "remove old",
        "retention", "expire data", "garbage collect", "vacuum",
    ],
    "start_daemon": [
        "start daemon", "start collector", "run daemon",
        "launch daemon", "start collecting",
    ],
    "stop_daemon": [
        "stop daemon", "stop collector", "halt daemon", "kill daemon",
        "stop the daemon", "stop the collector", "stop a daemon",
        "shut down daemon", "shutdown daemon",
    ],
    "daemon_status": [
        "daemon status", "is daemon running", "daemon info",
        "collector status", "daemon state",
    ],
    "validate_config": [
        "validate config", "validate configuration", "check config",
        "configuration", "is config ok", "verify config",
    ],
    "list_daemons": [
        "list daemons", "list collectors", "show daemons",
        "running daemons", "active collectors",
    ],
    "cleanup_report": [
        "cleanup report", "retention report", "cleanup history",
        "cleanup summary", "what was cleaned",
    ],
    "workflow": [
        "workflow", "stage", "progress", "next step", "where are we",
        "advance", "transition",
    ],
    "general": [],
}


def _keyword_intent(text: str) -> str:
    """Fast keyword-based intent detection."""
    text_lower = text.lower()
    best_intent = "general"
    best_score = 0
    for intent, keywords in _KEYWORD_INTENTS.items():
        if intent == "general":
            continue
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > best_score:
            best_score = score
            best_intent = intent
    return best_intent if best_score > 0 else "general"


def _extract_params(text: str, intent: str) -> dict[str, Any]:
    """Extract structured parameters from user text based on intent."""
    params: dict[str, Any] = {}
    text_lower = text.lower()

    # Instance name extraction.
    instance_match = re.search(r"instance[:\s=]+([\w-]+)", text, re.IGNORECASE)
    if instance_match:
        params["instance"] = instance_match.group(1)
    elif re.search(r"pg-\w+-\d+", text):
        m = re.search(r"(pg-\w+-\d+)", text)
        if m:
            params["instance"] = m.group(1)

    # Metric name extraction.
    metric_keywords = ["cpu", "memory", "disk", "connections", "queries", "latency", "throughput"]
    for metric in metric_keywords:
        if metric in text_lower:
            params["metric"] = metric
            break

    # Duration extraction.
    duration_match = re.search(r"(\d+)\s*(seconds?|sec|minutes?|min|hours?|h)", text, re.IGNORECASE)
    if duration_match:
        value = int(duration_match.group(1))
        unit = duration_match.group(2).lower()
        if unit.startswith("min"):
            params["duration_sec"] = value * 60
        elif unit.startswith("h"):
            params["duration_sec"] = value * 3600
        else:
            params["duration_sec"] = value

    # Source extraction.
    if "pgstat" in text_lower or "postgres" in text_lower or "postgresql" in text_lower:
        params["source"] = "pgstat"
    elif "mock" in text_lower:
        params["source"] = "mock"

    # Dry run.
    if "dry run" in text_lower or "dry-run" in text_lower:
        params["dry_run"] = True

    # Auto-trace.
    if "auto" in text_lower and "trace" in text_lower:
        params["auto_trace"] = True

    # All instances.
    if "all" in text_lower and ("daemon" in text_lower or "collector" in text_lower):
        params["all_instances"] = True

    return params


class IntentRecognizer:
    """Hybrid intent recognizer using keyword matching + LLM for ambiguous cases."""

    def __init__(self, provider: BaseProvider | None = None) -> None:
        self._provider = provider

    def recognize(self, text: str, use_llm: bool = False) -> tuple[str, dict[str, Any]]:
        """Recognize intent and extract parameters.

        Args:
            text: User input text
            use_llm: If True, use LLM for ambiguous cases (keyword-only if no provider)

        Returns:
            (intent_name, extracted_parameters)
        """
        # Fast keyword path.
        keyword_intent = _keyword_intent(text)
        params = _extract_params(text, keyword_intent)

        # If high confidence keyword match, skip LLM.
        high_confidence_intents = ["collect_metrics", "query_metrics", "health_check",
                                    "cleanup_data", "start_daemon", "stop_daemon",
                                    "daemon_status", "validate_config", "list_daemons",
                                    "cleanup_report"]
        if keyword_intent in high_confidence_intents and self._provider is None:
            return keyword_intent, params

        # LLM fallback for ambiguous or low-confidence cases.
        if use_llm and self._provider is not None:
            llm_intent = self._llm_intent(text)
            if llm_intent != "general":
                return llm_intent, params

        return keyword_intent, params

    def _llm_intent(self, text: str) -> str:
        """Use LLM to determine intent."""
        system_msg = (
            "You are a DBK (Database Kernel observability) intent classifier. "
            "Given a user message about database observability, classify it into one of: "
            "collect_metrics, query_metrics, health_check, diagnose_incident, run_trace, "
            "cleanup_data, start_daemon, stop_daemon, daemon_status, validate_config, "
            "list_daemons, cleanup_report, workflow, or general. "
            "Respond with ONLY the intent name, nothing else."
        )
        messages = [
            CompletionMessage(role="system", content=system_msg),
            CompletionMessage(role="user", content=text),
        ]
        try:
            response = self._provider.chat(messages)  # type: ignore[union-attr]
            intent = response.content.strip().lower().replace(" ", "_")
            # Validate the intent is known.
            all_intents = set(_KEYWORD_INTENTS.keys())
            if intent in all_intents:
                return intent
            return "general"
        except Exception:  # noqa: BLE001
            return "general"
