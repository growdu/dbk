"""DBK Provider Layer — LLM providers + Runtime providers.

LLM providers (dbk.providers.base.BaseProvider):
  MockProvider / OpenAIProvider / AnthropicProvider

Runtime providers (dbk.providers.runtime):
  MetricsProvider  — collect + query runtime metrics (mock / pgstat / prometheus)
  StorageProvider  — persist runtime data (sqlite / influxdb)
  AlertProvider    — evaluate alert rules (sqlite / influxdb)
"""
from __future__ import annotations

import os

from dbk.config_loader import TOMLConfig, TOMLError

# Import all providers so they can be imported directly from dbk.providers.
from dbk.providers.base import BaseProvider, CompletionMessage, CompletionResponse, ProviderError
from dbk.providers.mock import MockProvider
from dbk.providers.openai import OpenAIProvider
from dbk.providers.anthropic import AnthropicProvider
from dbk.providers.runtime import (
    MetricsProvider,
    StorageProvider,
    AlertProvider,
    MetricPoint,
    MetricsResult,
    QueryResult,
    AlertRule,
    AlertEvaluation,
)


def _toml_provider() -> str | None:
    """Return provider from ~/.dbk/config.toml, or None."""
    try:
        cfg = TOMLConfig.get_instance()
        provider = cfg.get("dbk", "provider")
        if isinstance(provider, str) and provider.strip():
            return provider.strip().lower()
    except TOMLError:
        pass
    return None


def auto_select_provider() -> BaseProvider:
    """Auto-select provider based on environment variables and config.toml.

    Priority:
    1. DBK_PROVIDER env var (force a specific provider)
    2. ~/.dbk/config.toml [dbk] provider
    3. DBK_ANTHROPIC_API_KEY -> AnthropicProvider
    4. DBK_OPENAI_API_KEY -> OpenAIProvider
    5. Fallback -> MockProvider
    """
    # Explicit override via env var.
    forced = os.environ.get("DBK_PROVIDER", "").lower().strip()
    if forced == "anthropic":
        return AnthropicProvider()
    if forced == "openai":
        return OpenAIProvider()
    if forced == "mock":
        return MockProvider()

    # Check config.toml [dbk] provider.
    if (toml_provider := _toml_provider()) is not None:
        if toml_provider == "anthropic":
            return AnthropicProvider()
        if toml_provider == "openai":
            return OpenAIProvider()
        if toml_provider == "mock":
            return MockProvider()

    # Auto-detect based on API keys (check both DBK_ prefixed and raw env vars).
    if os.environ.get("DBK_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return AnthropicProvider()
    if os.environ.get("DBK_OPENAI_API_KEY"):
        return OpenAIProvider()

    # Graceful degradation: no API key available.
    return MockProvider()


def get_provider() -> BaseProvider:
    """Get the active provider (singleton per process)."""
    return auto_select_provider()


__all__ = [
    # LLM providers
    "BaseProvider",
    "CompletionMessage",
    "CompletionResponse",
    "ProviderError",
    "MockProvider",
    "OpenAIProvider",
    "AnthropicProvider",
    # Runtime providers
    "MetricsProvider",
    "StorageProvider",
    "AlertProvider",
    "MetricPoint",
    "MetricsResult",
    "QueryResult",
    "AlertRule",
    "AlertEvaluation",
    # Entry points
    "auto_select_provider",
    "get_provider",
]
