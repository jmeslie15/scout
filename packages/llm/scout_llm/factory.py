"""Build a configured gateway from the environment.

This module is the only place that constructs providers (Rule 2). Reading configuration is
kept out of ``scout_llm.gateway`` so that the gateway stays a pure, injectable object and its
tests never touch ``os.environ``.

Configuration surface (names fixed by ``.env.example``):

==================== ======================= ============================
Variable             Default                 Meaning
==================== ======================= ============================
``SCOUT_LLM_DEFAULT_PROVIDER`` ``fake``     provider for calls with no override
``SCOUT_LLM_DEFAULT_MODEL``    ``fake-deterministic`` model for calls with no override
``SCOUT_OPENAI_API_KEY``       empty       enables the OpenAI provider
``SCOUT_ANTHROPIC_API_KEY``    empty       enables the Anthropic provider
``SCOUT_LLM_MAX_CONCURRENT_CALLS`` ``4``   semaphore ceiling
``SCOUT_LLM_REQUEST_TIMEOUT_SECONDS`` ``120`` per-request timeout
==================== ======================= ============================

**Offline by default.** A real provider is registered only when its key is present. If the
configured default provider has no key — or names a provider Scout does not implement — the
factory logs a prominent warning and falls back to :class:`~scout_llm.providers.fake.FakeProvider`
instead of raising. A fresh clone with an empty ``.env`` must run end to end.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from scout_domain.errors import ConfigurationError
from scout_domain.state.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from scout_llm.gateway import LLMGateway, ModelRequestRecorder
from scout_llm.observability import get_logger
from scout_llm.prompts import PromptLibrary
from scout_llm.providers.anthropic import AnthropicProvider
from scout_llm.providers.base import LLMProvider
from scout_llm.providers.fake import FakeProvider
from scout_llm.providers.openai import OpenAIProvider
from scout_llm.types import ModelPricing, ModelTarget

__all__ = [
    "ENV_ANTHROPIC_API_KEY",
    "ENV_DEFAULT_MODEL",
    "ENV_DEFAULT_PROVIDER",
    "ENV_MAX_CONCURRENT_CALLS",
    "ENV_OPENAI_API_KEY",
    "ENV_REQUEST_TIMEOUT_SECONDS",
    "build_gateway_from_env",
    "default_prompts_root",
]

logger = get_logger(__name__)

ENV_DEFAULT_PROVIDER = "SCOUT_LLM_DEFAULT_PROVIDER"
ENV_DEFAULT_MODEL = "SCOUT_LLM_DEFAULT_MODEL"
ENV_OPENAI_API_KEY = "SCOUT_OPENAI_API_KEY"
ENV_ANTHROPIC_API_KEY = "SCOUT_ANTHROPIC_API_KEY"
ENV_MAX_CONCURRENT_CALLS = "SCOUT_LLM_MAX_CONCURRENT_CALLS"
ENV_REQUEST_TIMEOUT_SECONDS = "SCOUT_LLM_REQUEST_TIMEOUT_SECONDS"

_DEFAULT_PROVIDER = "fake"
_DEFAULT_MODEL = "fake-deterministic"
_DEFAULT_MAX_CONCURRENT_CALLS = 4
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0

#: Repository root is three directories above this file:
#: ``<root>/packages/llm/scout_llm/factory.py``.
_ROOT_OFFSET = 3


def default_prompts_root() -> Path:
    """Return the repository's ``prompts/`` directory.

    Computed from this file's location rather than an environment variable, because the
    prompts tree ships with the code and a second source of truth would only be able to
    disagree with it. Callers that need a different tree pass ``prompts_root`` explicitly.
    """
    return Path(__file__).resolve().parents[_ROOT_OFFSET] / "prompts"


def build_gateway_from_env(
    *,
    environ: Mapping[str, str] | None = None,
    prompts_root: Path | None = None,
    prompts: PromptLibrary | None = None,
    recorder: ModelRequestRecorder | None = None,
    pricing: Mapping[str, ModelPricing] | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> LLMGateway:
    """Construct an :class:`~scout_llm.gateway.LLMGateway` from process configuration.

    Args:
        environ: Configuration source. Defaults to ``os.environ``; injectable so tests do
            not have to mutate global state.
        prompts_root: Prompt tree to read. Defaults to the repository's ``prompts/``.
        prompts: A pre-built prompt library, taking precedence over ``prompts_root``.
        recorder: Accounting sink. Defaults to the gateway's no-op recorder.
        pricing: Override the pricing table.
        retry_policy: Override Scout's default retry policy.

    Raises:
        ConfigurationError: a numeric setting is present but not parseable, or a limit is
            not positive. A malformed *number* fails loudly; a missing *credential* falls
            back to the offline provider, because the two are not equally ambiguous.
    """
    env = os.environ if environ is None else environ

    max_concurrent_calls = _int_setting(
        env,
        ENV_MAX_CONCURRENT_CALLS,
        default=_DEFAULT_MAX_CONCURRENT_CALLS,
    )
    if max_concurrent_calls < 1:
        raise ConfigurationError(
            f"{ENV_MAX_CONCURRENT_CALLS} must be at least 1, got {max_concurrent_calls}",
            variable=ENV_MAX_CONCURRENT_CALLS,
        )

    timeout_seconds = _float_setting(
        env,
        ENV_REQUEST_TIMEOUT_SECONDS,
        default=_DEFAULT_REQUEST_TIMEOUT_SECONDS,
    )
    if timeout_seconds <= 0:
        raise ConfigurationError(
            f"{ENV_REQUEST_TIMEOUT_SECONDS} must be positive, got {timeout_seconds}",
            variable=ENV_REQUEST_TIMEOUT_SECONDS,
        )

    providers: dict[str, LLMProvider] = {"fake": FakeProvider()}

    openai_key = (env.get(ENV_OPENAI_API_KEY) or "").strip()
    if openai_key:
        # Only constructed when a key exists: a deployment that never enables OpenAI never
        # builds its client, and therefore never opens a connection to it.
        providers["openai"] = OpenAIProvider(
            api_key=openai_key,
            timeout_seconds=timeout_seconds,
        )

    anthropic_key = (env.get(ENV_ANTHROPIC_API_KEY) or "").strip()
    if anthropic_key:
        providers["anthropic"] = AnthropicProvider(
            api_key=anthropic_key,
            timeout_seconds=timeout_seconds,
        )

    requested_provider = (env.get(ENV_DEFAULT_PROVIDER) or _DEFAULT_PROVIDER).strip().lower()
    default_model = (env.get(ENV_DEFAULT_MODEL) or "").strip() or _DEFAULT_MODEL

    if requested_provider in providers:
        default_provider = requested_provider
    else:
        reason = (
            f"no credentials present for provider {requested_provider!r}"
            if requested_provider in {"openai", "anthropic"}
            else f"unknown provider {requested_provider!r}"
        )
        logger.warning(
            "LLM gateway falling back to the offline 'fake' provider: %s. "
            "Set the matching API key (SCOUT_OPENAI_API_KEY / SCOUT_ANTHROPIC_API_KEY) to "
            "use a real provider. Scout remains fully functional offline; model output is "
            "reasoning, never evidence.",
            reason,
        )
        default_provider = _DEFAULT_PROVIDER

    library = (
        prompts
        if prompts is not None
        else PromptLibrary(prompts_root if prompts_root is not None else default_prompts_root())
    )

    logger.info(
        "LLM gateway configured: default_target=%s/%s providers=%s max_concurrent_calls=%d "
        "timeout=%.1fs",
        default_provider,
        default_model,
        sorted(providers),
        max_concurrent_calls,
        timeout_seconds,
    )

    return LLMGateway(
        providers=providers,
        default_target=ModelTarget(provider=default_provider, model=default_model),
        prompts=library,
        pricing=pricing,
        recorder=recorder,
        retry_policy=retry_policy,
        max_concurrent_calls=max_concurrent_calls,
    )


def _int_setting(env: Mapping[str, str], name: str, *, default: int) -> int:
    """Read an integer setting, raising a typed error on a malformed value."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must be an integer, got {raw!r}",
            variable=name,
            value=raw,
        ) from exc


def _float_setting(env: Mapping[str, str], name: str, *, default: float) -> float:
    """Read a float setting, raising a typed error on a malformed value."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must be a number, got {raw!r}",
            variable=name,
            value=raw,
        ) from exc
