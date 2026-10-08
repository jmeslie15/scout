"""OpenAI Chat Completions provider.

Wire contract (documented at ``https://platform.openai.com/docs/api-reference/chat``):

* ``POST {base_url}/chat/completions`` with ``Authorization: Bearer $SCOUT_OPENAI_API_KEY``
* request fields sent: ``model``, ``messages``, ``max_completion_tokens``, ``temperature``,
  ``stop``, and ``response_format`` — see :meth:`OpenAIProvider.build_body`
* response fields read: ``choices[0].message.content``,
  ``choices[0].finish_reason``, ``usage.prompt_tokens``, ``usage.completion_tokens``,
  ``usage.prompt_tokens_details.cached_tokens``

Decisions worth knowing about
-----------------------------

``max_output_tokens`` maps to ``max_completion_tokens``, not the older ``max_tokens``.
``max_tokens`` is deprecated by OpenAI and rejected by the newest reasoning models, so
sending it would make Scout subtly model-dependent.

No ``user`` or metadata field is transmitted. ``CompletionRequest.metadata`` is
gateway-local correlation data; inventing a vendor field for it would break the documented
schema (and the contract tests that pin it).

``response_format={"type": "json_object"}`` is sent for ``response_format="json"``. OpenAI
requires the word "JSON" to appear somewhere in the messages when this is used; that is the
prompt author's responsibility and is checked by prompt review, not rewritten here.
"""

from __future__ import annotations

import os
from typing import Any, Final

import httpx

from scout_llm.errors import LLMResponseFormatError
from scout_llm.providers.base import BaseHTTPProvider, as_int, as_str
from scout_llm.types import Completion, CompletionRequest, TokenUsage

__all__ = ["ENV_API_KEY", "OpenAIProvider"]

#: Environment variable holding the API key. The name is fixed by ``.env.example``.
ENV_API_KEY: Final = "SCOUT_OPENAI_API_KEY"

#: Documented base URL. The path appended by this provider is ``/chat/completions``.
DEFAULT_BASE_URL: Final = "https://api.openai.com/v1"

#: Context-overflow signals OpenAI documents on a 400 response.
_CONTEXT_OVERFLOW_CODES: Final[frozenset[str]] = frozenset(
    {"context_length_exceeded", "string_above_max_length"}
)


class OpenAIProvider(BaseHTTPProvider):
    """Talks to OpenAI's Chat Completions API. One HTTP request per ``complete`` call."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_key = api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")
        super().__init__(
            base_url=base_url,
            api_key=resolved_key,
            timeout_seconds=timeout_seconds,
            client=client,
        )

    def default_headers(self) -> dict[str, str]:
        """Bearer authentication. The key never appears in a log or a retained payload."""
        return {"authorization": f"Bearer {self._api_key}"}

    def build_body(self, request: CompletionRequest, *, model: str) -> dict[str, Any]:
        """Translate a :class:`CompletionRequest` into a Chat Completions body.

        Optional fields are omitted rather than sent as ``null`` so that the request
        matches the documented schema exactly.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": _wire_messages(request),
        }
        if request.max_output_tokens is not None:
            body["max_completion_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop:
            body["stop"] = list(request.stop)
        if request.response_format == "json":
            body["response_format"] = {"type": "json_object"}
        return body

    async def complete(self, request: CompletionRequest, *, model: str) -> Completion:
        """Perform one Chat Completions request. Never retries."""
        result = await self._post_json("/chat/completions", self.build_body(request, model=model))
        body = result.body

        text, finish_reason = _extract_choice(body)
        usage_block = body.get("usage")
        usage_source = usage_block if isinstance(usage_block, dict) else {}
        details = usage_source.get("prompt_tokens_details")
        details_source = details if isinstance(details, dict) else {}

        return Completion(
            text=text,
            usage=TokenUsage(
                # OpenAI's prompt_tokens already includes cached tokens, which is exactly
                # the normalisation rule documented in scout_llm.types.
                input_tokens=as_int(usage_source.get("prompt_tokens")),
                output_tokens=as_int(usage_source.get("completion_tokens")),
                cache_read_tokens=as_int(details_source.get("cached_tokens")),
                cache_write_tokens=0,
            ),
            provider=self.name,
            model=model,
            finish_reason=finish_reason,
            latency_seconds=result.latency_seconds,
            raw=self.raw_payload(result),
        )

    def _detects_context_overflow(self, *, status_code: int, body: Any) -> bool:
        """Recognise OpenAI's documented ``context_length_exceeded`` error code."""
        if status_code != httpx.codes.BAD_REQUEST or not isinstance(body, dict):
            return False
        error = body.get("error")
        if not isinstance(error, dict):
            return False
        return as_str(error.get("code")) in _CONTEXT_OVERFLOW_CODES


def _wire_messages(request: CompletionRequest) -> list[dict[str, str]]:
    """Render messages in OpenAI's ``{"role", "content"}`` shape."""
    return [{"role": message.role, "content": message.content} for message in request.messages]


def _extract_choice(body: dict[str, Any]) -> tuple[str, str | None]:
    """Pull ``(text, finish_reason)`` out of a Chat Completions response.

    Raises:
        LLMResponseFormatError: the body does not contain the documented shape. A missing
            ``choices`` array is a provider contract break, not an empty answer, and must
            not be reported as a successful completion.
    """
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseFormatError(
            "openai response contained no choices",
            provider="openai",
            body_keys=sorted(str(key) for key in body),
        )

    first = choices[0]
    if not isinstance(first, dict):
        raise LLMResponseFormatError("openai response choice was not an object", provider="openai")

    message: Any = first.get("message")
    if not isinstance(message, dict):
        raise LLMResponseFormatError(
            "openai response choice had no message object",
            provider="openai",
        )

    content = message.get("content")
    if not isinstance(content, str):
        raise LLMResponseFormatError(
            "openai response message had no text content",
            provider="openai",
            content_type=type(content).__name__,
        )

    return content, as_str(first.get("finish_reason"))
