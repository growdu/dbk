"""Anthropic (Claude) provider with MiniMax / OpenRouter / custom base_url support."""
from __future__ import annotations

import os
from typing import Any, Generator

from dbk.providers.base import (
    BaseProvider,
    CompletionMessage,
    CompletionResponse,
    ProviderAuthError,
    ProviderError,
    ProviderRetryableError,
)

# Defaults.
DEFAULT_BASE_URL = "https://api.minimaxi.com/anthropic"


def _get_anthropic_client(
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_sec: float = 60.0,
) -> Any:
    """Create an Anthropic client with optional custom base_url (MiniMax, OpenRouter, etc.)."""
    if api_key is None:
        api_key = os.environ.get("DBK_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""

    if not api_key:
        raise ProviderAuthError(
            "Anthropic API key not found. Set DBK_ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN."
        )

    if base_url is None:
        base_url = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("DBK_ANTHROPIC_BASE_URL")

    # Try anthropic>=0.20 first (newer API).
    try:
        import anthropic

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout_sec}
        if base_url:
            kwargs["base_url"] = base_url
        return anthropic.Anthropic(**kwargs)
    except ImportError:
        pass

    # Try anthropic<0.20 (legacy API).
    try:
        import anthropic as old_anthropic

        return old_anthropic
    except ImportError:
        raise ProviderError(
            "Anthropic package not installed. Install with: pip install anthropic"
        )


class AnthropicProvider(BaseProvider):
    """Anthropic Claude chat completion provider.

    Supports:
    - Standard Anthropic API (api.anthropic.com)
    - MiniMax Anthropic-compatible API (api.minimaxi.com/anthropic)
    - OpenRouter and other Anthropic-compatible proxies
    - Extended thinking models (thinking + text content blocks)
    """

    name: str = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        default_max_tokens: int = 4096,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._api_key = api_key
        self._base_url = base_url
        self._default_model = model or os.environ.get("DBK_MODEL") or "claude-3-5-haiku-20241022"
        self._default_max_tokens = default_max_tokens
        self._client: Any = None

    def _client_obj(self) -> Any:
        if self._client is None:
            self._client = _get_anthropic_client(
                self._api_key,
                self._base_url,
                self.timeout_sec,
            )
        return self._client

    def _is_new_api(self) -> bool:
        client = self._client_obj()
        return hasattr(client, "messages")

    def _to_api_format(
        self, messages: list[CompletionMessage]
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Convert messages to Anthropic API format. Returns (formatted_messages, system_prompt)."""
        formatted: list[dict[str, Any]] = []
        system_prompt: str | None = None

        for msg in messages:
            if msg.role == "system":
                system_prompt = msg.content
            else:
                role = "user" if msg.role == "user" else "assistant"
                item: dict[str, Any] = {"role": role, "content": msg.content}
                if msg.name:
                    item["author"] = msg.name
                formatted.append(item)

        return formatted, system_prompt

    def _extract_text(self, content: list[Any]) -> str:
        """Extract text from content blocks, skipping thinking blocks.

        Handles both standard Anthropic responses and extended thinking
        models (e.g., MiniMax M2.7) that return mixed content blocks.
        """
        parts: list[str] = []
        for block in content:
            if hasattr(block, "type"):
                block_type = block.type
            else:
                block_type = block.get("type") if isinstance(block, dict) else None

            if block_type == "text":
                text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None) or ""
                parts.append(text)
            # Skip thinking blocks — they're already processed by the model
        return " ".join(parts) if parts else ""

    def chat(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        effective_model = model or self._default_model
        formatted_messages, system_prompt = self._to_api_format(messages)

        try:
            client = self._client_obj()
            extra_kwargs = {k: v for k, v in kwargs.items() if k != "stream"}
            # Use caller-supplied max_tokens or default, minimum 256
            max_tokens = int(extra_kwargs.pop("max_tokens", self._default_max_tokens))
            max_tokens = max(max_tokens, 256)

            if self._is_new_api():
                request_params: dict[str, Any] = {
                    "model": effective_model,
                    "messages": formatted_messages,  # type: ignore[arg-type]
                    "max_tokens": max_tokens,
                    **extra_kwargs,
                }
                if system_prompt:
                    request_params["system"] = system_prompt

                response = client.messages.create(**request_params)

                # Extract text from content blocks (handles thinking blocks from MiniMax)
                content_text = self._extract_text(response.content)

                return CompletionResponse(
                    content=content_text,
                    model=response.model,
                    usage={
                        "prompt_tokens": response.usage.input_tokens if response.usage else 0,
                        "completion_tokens": response.usage.output_tokens if response.usage else 0,
                        "total_tokens": (
                            response.usage.input_tokens + response.usage.output_tokens
                            if response.usage
                            else 0
                        ),
                    },
                    finish_reason=str(response.stop_reason) if response.stop_reason else None,
                    raw=response.model_dump() if hasattr(response, "model_dump") else {},
                )
            else:
                # Legacy anthropic<0.20 API.
                params: dict[str, Any] = {
                    "model": effective_model,
                    "messages": formatted_messages,
                    "max_tokens": extra_kwargs.pop("max_tokens", 1024),
                    **extra_kwargs,
                }
                if system_prompt:
                    params["system"] = system_prompt

                response = client.completions.create(
                    prompt=client.messages.format(  # type: ignore[union-attr]
                        messages=formatted_messages
                    ),  # type: ignore[arg-type]
                    **params,
                )
                content = response.completion if hasattr(response, "completion") else str(response)
                return CompletionResponse(
                    content=content,
                    model=effective_model,
                    usage=None,
                    finish_reason=None,
                    raw={},
                )
        except Exception as exc:
            err_str = str(exc).lower()
            if "auth" in err_str or "api_key" in err_str or "401" in err_str or "403" in err_str:
                raise ProviderAuthError(f"Anthropic auth error: {exc}") from exc
            if "rate" in err_str or "429" in err_str or "500" in err_str or "502" in err_str or "503" in err_str:
                raise ProviderRetryableError(f"Anthropic retryable error: {exc}") from exc
            if "timeout" in err_str or "timed out" in err_str:
                raise ProviderRetryableError(f"Anthropic timeout: {exc}") from exc
            raise ProviderError(f"Anthropic error: {exc}") from exc

    def chat_stream(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        effective_model = model or self._default_model
        formatted_messages, system_prompt = self._to_api_format(messages)

        try:
            client = self._client_obj()
            extra_kwargs = {k: v for k, v in kwargs.items() if k != "stream"}
            max_tokens = int(extra_kwargs.pop("max_tokens", self._default_max_tokens))
            max_tokens = max(max_tokens, 256)

            if self._is_new_api():
                request_params: dict[str, Any] = {
                    "model": effective_model,
                    "messages": formatted_messages,  # type: ignore[arg-type]
                    "max_tokens": max_tokens,
                    "stream": True,
                    **extra_kwargs,
                }
                if system_prompt:
                    request_params["system"] = system_prompt

                with client.messages.stream(**request_params) as stream:
                    for event in stream:
                        if event.type == "content_block_delta":
                            delta = getattr(event.delta, "text", "") or ""
                            if delta:
                                yield delta
            else:
                # Legacy API - streaming not easily supported, fall back to non-streaming.
                response = self.chat(messages, model=effective_model, **kwargs)
                for word in response.content.split():
                    yield word + " "
        except Exception as exc:
            raise ProviderError(f"Anthropic stream error: {exc}") from exc
