"""Base provider abstraction for LLM backends."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generator, Literal

DEFAULT_TIMEOUT_SEC = 60.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SEC = 1.0
DEFAULT_BACKOFF_MAX_SEC = 10.0


class ProviderError(Exception):
    """Base exception for provider errors."""

    pass


class ProviderRetryableError(ProviderError):
    """Error that may succeed on retry (network, rate limit, etc.)."""

    pass


class ProviderAuthError(ProviderError):
    """Authentication/authorization failure."""

    pass


@dataclass(slots=True)
class CompletionMessage:
    role: Literal["system", "user", "assistant"]
    content: str
    name: str | None = None


@dataclass(slots=True)
class CompletionResponse:
    content: str
    raw: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    usage: dict[str, int] | None = None
    finish_reason: str | None = None


class BaseProvider(ABC):
    """Abstract base class for LLM providers."""

    name: str = "base"
    supports_streaming: bool = False

    def __init__(
        self,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SEC,
        backoff_max: float = DEFAULT_BACKOFF_MAX_SEC,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

    @abstractmethod
    def chat(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Send a chat completion request and return the response."""

    def chat_stream(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        """Stream chat completion tokens. Override if provider supports it."""
        raise NotImplementedError(f"{self.name} does not support streaming")

    def chat_with_retry(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        """Call chat() with exponential-backoff retry on retryable errors."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return self.chat(messages, model=model, **kwargs)
            except ProviderRetryableError as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    wait = min(self.backoff_base * (2 ** attempt), self.backoff_max)
                    time.sleep(wait)
            except ProviderAuthError:
                raise
            except ProviderError as exc:
                last_exc = exc
                if attempt < self.max_retries - 1:
                    wait = min(self.backoff_base * (2 ** attempt), self.backoff_max)
                    time.sleep(wait)
        if last_exc is not None:
            raise ProviderError(f"All {self.max_retries} retries exhausted: {last_exc}") from last_exc
        raise ProviderError("All retries exhausted with no exception")

    @property
    def is_mock(self) -> bool:
        """Return True for mock providers that don't require API keys."""
        return False
