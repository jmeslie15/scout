"""``scout_llm`` — Scout's provider-agnostic LLM gateway (spec sections 37, 38, 39).

Two properties define this package:

1. **It is the only component that talks to a model provider** (Rule 2). Nothing outside
   ``scout_llm`` may instantiate a provider client; application code obtains a configured
   :class:`~scout_llm.gateway.LLMGateway` from :func:`~scout_llm.factory.build_gateway_from_env`.
2. **No LLM output is ever deterministic evidence** (Rule 9). The gateway returns text,
   optional parsed JSON, and a :class:`~scout_domain.models.model_request.ModelRequestRecord`.
   It never decides a status, a risk level, or a recommendation.

Importing this package registers the gateway's exception hierarchy with
``scout_domain.state.retry`` so retry classification works for callers that only import the
domain layer.

Typical use::

    gateway = build_gateway_from_env()
    result = await gateway.complete(AgentRole.CODE_REVIEWER, "v1", {"diff": diff_text})
    result, payload = await gateway.complete_json(
        AgentRole.SECURITY, "v1", {"diff": diff_text}
    )
"""

from __future__ import annotations

from scout_llm.errors import (
    LLMAuthenticationError,
    LLMContextTooLargeError,
    LLMGatewayError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
    PromptNotFoundError,
    PromptRenderError,
)
from scout_llm.factory import build_gateway_from_env, default_prompts_root
from scout_llm.gateway import (
    GatewayResult,
    InMemoryRecorder,
    LLMGateway,
    ModelRequestRecorder,
    NullRecorder,
)
from scout_llm.json_extraction import extract_json_object, try_parse_json_object
from scout_llm.pricing import PRICING, ZERO_PRICING, price_for
from scout_llm.prompts import PromptLibrary, PromptTemplate
from scout_llm.providers.anthropic import AnthropicProvider
from scout_llm.providers.base import LLMProvider
from scout_llm.providers.fake import FakeProvider
from scout_llm.providers.openai import OpenAIProvider
from scout_llm.redaction import redact_headers, redact_payload, redact_text
from scout_llm.types import (
    Completion,
    CompletionRequest,
    Message,
    ModelPricing,
    ModelTarget,
    ResponseFormat,
    TokenUsage,
)

__all__ = [
    "PRICING",
    "ZERO_PRICING",
    "AnthropicProvider",
    "Completion",
    "CompletionRequest",
    "FakeProvider",
    "GatewayResult",
    "InMemoryRecorder",
    "LLMAuthenticationError",
    "LLMContextTooLargeError",
    "LLMGateway",
    "LLMGatewayError",
    "LLMInvalidRequestError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMResponseFormatError",
    "LLMTimeoutError",
    "LLMTransientError",
    "Message",
    "ModelPricing",
    "ModelRequestRecorder",
    "ModelTarget",
    "NullRecorder",
    "OpenAIProvider",
    "PromptLibrary",
    "PromptNotFoundError",
    "PromptRenderError",
    "PromptTemplate",
    "ResponseFormat",
    "TokenUsage",
    "build_gateway_from_env",
    "default_prompts_root",
    "extract_json_object",
    "price_for",
    "redact_headers",
    "redact_payload",
    "redact_text",
    "try_parse_json_object",
]
