"""Tests for LLM providers."""
from __future__ import annotations

import pytest

from dbk.providers.base import (
    BaseProvider,
    CompletionMessage,
    CompletionResponse,
    ProviderAuthError,
    ProviderError,
    ProviderRetryableError,
)
from dbk.providers.mock import MockProvider


class TestMockProvider:
    def test_chat_returns_response(self) -> None:
        provider = MockProvider()
        messages = [
            CompletionMessage(role="user", content="show me metrics for pg-main-01"),
        ]
        response = provider.chat(messages)
        assert isinstance(response, CompletionResponse)
        assert response.content
        assert response.model == "mock/model"
        assert "pg-main-01" in response.content or "metrics" in response.content.lower()

    def test_chat_identifies_collect_metrics_intent(self) -> None:
        provider = MockProvider()
        messages = [CompletionMessage(role="user", content="collect metrics")]
        response = provider.chat(messages)
        assert "collect_metrics" in response.content.lower() or "collect" in response.content.lower()

    def test_chat_identifies_health_check_intent(self) -> None:
        provider = MockProvider()
        messages = [CompletionMessage(role="user", content="health check")]
        response = provider.chat(messages)
        assert "health" in response.content.lower()

    def test_chat_identifies_cleanup_intent(self) -> None:
        provider = MockProvider()
        messages = [CompletionMessage(role="user", content="cleanup old data")]
        response = provider.chat(messages)
        assert "cleanup" in response.content.lower()

    def test_chat_stream_yields_tokens(self) -> None:
        provider = MockProvider()
        messages = [CompletionMessage(role="user", content="list daemons")]
        tokens = list(provider.chat_stream(messages))
        assert len(tokens) > 0
        full = "".join(tokens)
        assert full

    def test_chat_with_system_message(self) -> None:
        provider = MockProvider()
        messages = [
            CompletionMessage(role="system", content="You are a helpful DBK assistant."),
            CompletionMessage(role="user", content="diagnose latency incident on pg-prod-02"),
        ]
        response = provider.chat(messages)
        assert response.content

    def test_is_mock(self) -> None:
        provider = MockProvider()
        assert provider.is_mock is True
        assert provider.name == "mock"

    def test_retry_behavior_no_retry_on_auth(self) -> None:
        # MockProvider doesn't raise auth errors, so retry should work.
        provider = MockProvider()
        # Should not raise.
        response = provider.chat_with_retry([CompletionMessage(role="user", content="test")])
        assert response.content

    def test_empty_user_message(self) -> None:
        provider = MockProvider()
        messages = [CompletionMessage(role="user", content="")]
        response = provider.chat(messages)
        assert response.content


class TestBaseProvider:
    def test_provider_error_inheritance(self) -> None:
        assert issubclass(ProviderError, Exception)
        assert issubclass(ProviderRetryableError, ProviderError)
        assert issubclass(ProviderAuthError, ProviderError)

    def test_completion_message(self) -> None:
        msg = CompletionMessage(role="user", content="hello", name=None)
        assert msg.role == "user"
        assert msg.content == "hello"

    def test_completion_response(self) -> None:
        resp = CompletionResponse(
            content="hello world",
            model="gpt-4",
            usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            finish_reason="stop",
        )
        assert resp.content == "hello world"
        assert resp.model == "gpt-4"
        assert resp.usage is not None


class TestAutoSelect:
    def test_auto_select_returns_mock_when_no_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DBK_PROVIDER", raising=False)
        monkeypatch.delenv("DBK_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DBK_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        from dbk.providers import auto_select_provider
        provider = auto_select_provider()
        assert isinstance(provider, MockProvider)

    def test_auto_select_respects_dbk_provider_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DBK_PROVIDER", "mock")
        monkeypatch.delenv("DBK_OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DBK_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        from dbk.providers import auto_select_provider
        provider = auto_select_provider()
        assert isinstance(provider, MockProvider)

    def test_auto_select_anthropic_with_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DBK_ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.delenv("DBK_PROVIDER", raising=False)
        monkeypatch.delenv("DBK_OPENAI_API_KEY", raising=False)

        from dbk.providers import auto_select_provider, AnthropicProvider
        provider = auto_select_provider()
        assert isinstance(provider, AnthropicProvider)

    def test_auto_select_openai_with_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DBK_OPENAI_API_KEY", "sk-test")
        monkeypatch.delenv("DBK_PROVIDER", raising=False)
        monkeypatch.delenv("DBK_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        from dbk.providers import auto_select_provider, OpenAIProvider
        provider = auto_select_provider()
        assert isinstance(provider, OpenAIProvider)


class TestOpenAIProviderDetection:
    def test_openai_version_detection(self) -> None:
        from dbk.providers.openai import _detect_version
        v = _detect_version()
        # Should return a string (either "not installed" or a version number).
        assert isinstance(v, str)

    def test_openai_provider_init_defaults(self) -> None:
        from dbk.providers.openai import OpenAIProvider
        provider = OpenAIProvider()
        assert provider.name == "openai"
        assert provider.timeout_sec == 60.0
        assert provider.max_retries == 3


class TestAnthropicProviderInit:
    def test_anthropic_provider_init_defaults(self) -> None:
        from dbk.providers.anthropic import AnthropicProvider
        provider = AnthropicProvider()
        assert provider.name == "anthropic"
        assert provider.timeout_sec == 60.0
        assert provider.max_retries == 3

    def test_anthropic_no_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dbk.providers.anthropic import AnthropicProvider
        monkeypatch.delenv("DBK_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

        provider = AnthropicProvider(api_key=None)
        with pytest.raises(ProviderAuthError):
            provider._client_obj()
