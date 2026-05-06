"""OpenAI provider supporting both openai<1.0 and openai>=1.0 client APIs."""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Generator

from dbk.providers.base import (
    BaseProvider,
    CompletionMessage,
    CompletionResponse,
    ProviderAuthError,
    ProviderError,
    ProviderRetryableError,
)

# Detect openai package version.
_OPENAI_V1: bool
try:
    import openai

    _v = getattr(openai, "__version__", "0.0.0")
    _OPENAI_V1 = not _v.startswith("0.") and not _v.startswith("1.0")
    del openai
except ImportError:
    _OPENAI_V1 = False


def _get_openai_client(
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_sec: float = 60.0,
) -> Any:
    """Create an OpenAI client, auto-detecting the installed version."""
    from dbk.config import provider_openai_api_key

    if api_key is None:
        api_key = provider_openai_api_key()

    if not api_key:
        raise ProviderAuthError("OpenAI API key not found. Set DBK_OPENAI_API_KEY, OPENAI_API_KEY, or [providers] openai_api_key in config.")

    if _OPENAI_V1:
        import openai

        extra_kwargs: dict[str, Any] = {}
        if base_url:
            extra_kwargs["base_url"] = base_url
        return openai.OpenAI(api_key=api_key, timeout=timeout_sec, **extra_kwargs)
    else:
        import openai

        return openai.ChatCompletion if hasattr(openai, "ChatCompletion") else None


def _detect_version() -> str:
    try:
        import openai

        v = getattr(openai, "__version__", "unknown")
        return v
    except ImportError:
        return "not installed"


class OpenAIProvider(BaseProvider):
    """OpenAI chat completion provider.

    Supports both openai<1.0 (sync mode with ChatCompletion.create)
    and openai>=1.0 (sync mode with OpenAI().chat.completions.create).
    """

    name: str = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._api_key = api_key
        from dbk.config import provider_openai_model

        self._default_model = model or os.environ.get("DBK_MODEL") or provider_openai_model()
        self._base_url = base_url
        self._client: Any = None

    @property
    def version_info(self) -> str:
        return _detect_version()

    def _client_obj(self) -> Any:
        if self._client is None:
            self._client = _get_openai_client(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout_sec=self.timeout_sec,
            )
        return self._client

    def _to_api_messages(
        self, messages: list[CompletionMessage]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for msg in messages:
            item: dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.name:
                item["name"] = msg.name
            result.append(item)
        return result

    def chat(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs: Any,
    ) -> CompletionResponse:
        effective_model = model or self._default_model
        api_messages = self._to_api_messages(messages)

        try:
            if _OPENAI_V1:
                client = self._client_obj()
                response = client.chat.completions.create(
                    model=effective_model,
                    messages=api_messages,
                    **kwargs,
                )
                content = response.choices[0].message.content or ""
                return CompletionResponse(
                    content=content,
                    model=response.model,
                    usage={
                        "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                        "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                        "total_tokens": response.usage.total_tokens if response.usage else 0,
                    },
                    finish_reason=response.choices[0].finish_reason,
                    raw=response.model_dump() if hasattr(response, "model_dump") else {},
                )
            else:
                import openai

                params: dict[str, Any] = {
                    "model": effective_model,
                    "messages": api_messages,
                }
                params.update(kwargs)
                # For openai<1.0, the client is accessed via the module directly.
                if hasattr(openai, "ChatCompletion"):
                    response = openai.ChatCompletion.create(**params)
                else:
                    raise ProviderError(
                        "openai package not found or unusable. "
                        f"Version detected: {_detect_version()}"
                    )
                choice = response["choices"][0]
                return CompletionResponse(
                    content=choice["message"]["content"] or "",
                    model=response.get("model"),
                    usage=response.get("usage"),
                    finish_reason=choice.get("finish_reason"),
                    raw=response,
                )
        except Exception as exc:
            err_str = str(exc).lower()
            if "auth" in err_str or "api key" in err_str or "401" in err_str:
                raise ProviderAuthError(f"OpenAI auth error: {exc}") from exc
            if "rate" in err_str or "429" in err_str or "500" in err_str or "502" in err_str or "503" in err_str:
                raise ProviderRetryableError(f"OpenAI retryable error: {exc}") from exc
            if "timeout" in err_str or "timed out" in err_str:
                raise ProviderRetryableError(f"OpenAI timeout: {exc}") from exc
            raise ProviderError(f"OpenAI error: {exc}") from exc

    def chat_stream(
        self,
        messages: list[CompletionMessage],
        model: str | None = None,
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        effective_model = model or self._default_model
        api_messages = self._to_api_messages(messages)

        try:
            if _OPENAI_V1:
                client = self._client_obj()
                stream = client.chat.completions.create(
                    model=effective_model,
                    messages=api_messages,
                    stream=True,
                    **kwargs,
                )
                for chunk in stream:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield delta
            else:
                import openai

                params: dict[str, Any] = {
                    "model": effective_model,
                    "messages": api_messages,
                    "stream": True,
                }
                params.update(kwargs)
                if hasattr(openai, "ChatCompletion"):
                    stream = openai.ChatCompletion.create(**params)
                    for chunk in stream:
                        if "choices" in chunk and chunk["choices"]:
                            delta = chunk["choices"][0].get("delta", {}).get("content", "")
                            if delta:
                                yield delta
                else:
                    raise ProviderError("openai not available")
        except Exception as exc:
            raise ProviderError(f"OpenAI stream error: {exc}") from exc
