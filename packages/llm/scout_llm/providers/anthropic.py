"""Anthropic Messages provider.

Wire contract (documented at ``https://docs.anthropic.com/en/api/messages``):

* ``POST {base_url}/v1/messages``
* headers ``x-api-key: $SCOUT_ANTHROPIC_API_KEY`` and
  ``anthropic-version: 2023-06-01`` (the version header is required; omitting it is an
  error rather than a default)
* request fields sent: ``model``, ``max_tokens``, ``system``, ``messages``,
  ``temperature``, ``stop_sequences``
* response fields read: ``content[].text`` (concatenated), ``stop_reason``,
  ``usage.input_tokens``, ``usage.output_tokens``, ``usage.cache_read_input_tokens``,
  ``usage.cache_creation_input_tokens``

Decisions worth knowing about
-----------------------------

**The system prompt is a top-level field, not a message.** Anthropic rejects a message with
``role="system"``. Scout's transport-agnostic :class:`CompletionRequest` uses
``Message(role="system", ...)`` because that is the shape both OpenAI and the prompt
library speak, so this provider hoists every system message into the top-level ``system``
field and joins multiple ones with a blank line. The remaining messages keep their order.

**``max_tokens`` is required by Anthropic** and has no server-side default. When the caller
does not set ``max_output_tokens`` this provider sends
:data:`DEFAULT_MAX_OUTPUT_TOKENS` rather than omitting the field, because a request without
it is rejected outright.

**Anthropic has no ``response_format`` JSON mode.** Scout does not invent one: the gateway's
balanced-brace extraction handles prose- or fence-wrapped JSON, which is what actually
works against the Messages API.

**Cache tokens are folded into ``input_tokens``** per the normalisation rule in
``scout_llm.types``, because Anthropic reports them *outside* ``usage.input_tokens``.
"""

from __future__ import annotations

import os
from typing import Any, Final

import httpx

from scout_llm.errors import LLMInvalidRequestError, LLMResponseFormatError
from scout_llm.providers.base import BaseHTTPProvider, as_int, as_str
from scout_llm.types import Completion, CompletionRequest, TokenUsage

__all__ = ["ENV_API_KEY", "AnthropicProvider"]

#: Environment variable holding the API key. The name is fixed by ``.env.example``.
ENV_API_KEY: Final = "SCOUT_ANTHROPIC_API_KEY"

#: Documented base URL. The path appended by this provider is ``/v1/messages``.
DEFAULT_BASE_URL: Final = "https://api.anthropic.com"

#: Required API version header. Pinned rather than configurable so a vendor default change
#: cannot silently alter Scout's request/response contract.
ANTHROPIC_VERSION: Final = "2023-06-01"

#: Sent when the caller does not specify a limit. Anthropic requires ``max_tokens``, and
#: 4096 is a conservative ceiling that does not truncate a normal review response.
DEFAULT_MAX_OUTPUT_TOKENS: Final = 4096


class AnthropicProvider(BaseHTTPProvider):
    """Talks to Anthropic's Messages API. One HTTP request per ``complete`` call."""

    name = "anthropic"

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
        """``x-api-key`` plus the mandatory version header."""
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    def build_body(self, request: CompletionRequest, *, model: str) -> dict[str, Any]:
        """Translate a :class:`CompletionRequest` into a Messages body."""
        system_parts = [message.content for message in request.messages if message.role == "system"]
        conversation = [
            {"role": message.role, "content": message.content}
            for message in request.messages
            if message.role != "system"
        ]
        if not conversation:
            raise LLMInvalidRequestError(
                "anthropic requires at least one non-system message; the request "
                "contained only system messages",
                provider=self.name,
            )

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": (
                request.max_output_tokens
                if request.max_output_tokens is not None
                else DEFAULT_MAX_OUTPUT_TOKENS
            ),
            "messages": conversation,
        }
        if system_parts:
            # Anthropic's documented shape for a system prompt: a top-level field. A list
            # of blocks is also accepted; a single joined string is used because Scout
            # never needs per-block cache control here.
            body["system"] = "\n\n".join(system_parts)
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop:
            body["stop_sequences"] = list(request.stop)
        return body

    async def complete(self, request: CompletionRequest, *, model: str) -> Completion:
        """Perform one Messages request. Never retries."""
        result = await self._post_json("/v1/messages", self.build_body(request, model=model))
        body = result.body

        text = _extract_text(body)
        usage_block = body.get("usage")
        usage_source = usage_block if isinstance(usage_block, dict) else {}

        cache_read = as_int(usage_source.get("cache_read_input_tokens"))
        cache_write = as_int(usage_source.get("cache_creation_input_tokens"))

        return Completion(
            text=text,
            usage=TokenUsage(
                # Anthropic reports cache tokens outside input_tokens, so they are added
                # back to keep "input_tokens is the total prompt" true (see
                # scout_llm.types).
                input_tokens=as_int(usage_source.get("input_tokens")) + cache_read + cache_write,
                output_tokens=as_int(usage_source.get("output_tokens")),
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            ),
            provider=self.name,
            model=model,
            finish_reason=as_str(body.get("stop_reason")),
            latency_seconds=result.latency_seconds,
            raw=self.raw_payload(result),
        )

    def _detects_context_overflow(self, *, status_code: int, body: Any) -> bool:
        """Recognise Anthropic's documented "prompt is too long" rejection.

        Anthropic returns 400 with ``error.type == "invalid_request_error"`` and a message
        that begins ``prompt is too long``. The message text is checked *only* to choose a
        clearer exception type; both candidates are non-retryable, so a wording change at
        the vendor cannot turn this into a retry storm (Rule 12).
        """
        if status_code != httpx.codes.BAD_REQUEST or not isinstance(body, dict):
            return False
        error = body.get("error")
        if not isinstance(error, dict):
            return False
        message = as_str(error.get("message")) or ""
        return "prompt is too long" in message.lower()


def _extract_text(body: dict[str, Any]) -> str:
    """Concatenate the text blocks of a Messages response.

    Raises:
        LLMResponseFormatError: the response has no usable ``content`` array. Anthropic
            returns a non-text block (for example ``tool_use``) when the request asked for
            tools, which Scout never does; treating it as an error rather than as empty
            text keeps a contract break visible.
    """
    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise LLMResponseFormatError(
            "anthropic response contained no content blocks",
            provider="anthropic",
            body_keys=sorted(str(key) for key in body),
        )

    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = as_str(block.get("text"))
            if text is not None:
                parts.append(text)

    if not parts:
        raise LLMResponseFormatError(
            "anthropic response contained no text blocks",
            provider="anthropic",
            block_types=sorted(
                str(block.get("type")) for block in content if isinstance(block, dict)
            ),
        )
    return "".join(parts)
