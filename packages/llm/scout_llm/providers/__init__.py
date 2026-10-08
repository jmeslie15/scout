"""Provider implementations for the LLM gateway.

Importing from this package is the only supported way to obtain a provider. Nothing outside
``scout_llm`` should construct one (Rule 2); application code receives a configured
:class:`~scout_llm.gateway.LLMGateway` from :func:`scout_llm.factory.build_gateway_from_env`.
"""

from __future__ import annotations

from scout_llm.providers.anthropic import AnthropicProvider
from scout_llm.providers.base import BaseHTTPProvider, LLMProvider, parse_retry_after
from scout_llm.providers.fake import CHARS_PER_TOKEN, FakeProvider
from scout_llm.providers.openai import OpenAIProvider

__all__ = [
    "CHARS_PER_TOKEN",
    "AnthropicProvider",
    "BaseHTTPProvider",
    "FakeProvider",
    "LLMProvider",
    "OpenAIProvider",
    "parse_retry_after",
]
