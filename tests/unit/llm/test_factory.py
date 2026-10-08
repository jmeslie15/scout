"""Environment-driven construction, and the offline-by-default guarantee."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from scout_domain.enums import AgentRole
from scout_domain.errors import ConfigurationError
from scout_domain.state.retry import RetryPolicy
from scout_llm.factory import (
    ENV_ANTHROPIC_API_KEY,
    ENV_DEFAULT_MODEL,
    ENV_DEFAULT_PROVIDER,
    ENV_MAX_CONCURRENT_CALLS,
    ENV_OPENAI_API_KEY,
    ENV_REQUEST_TIMEOUT_SECONDS,
    build_gateway_from_env,
    default_prompts_root,
)
from scout_llm.gateway import InMemoryRecorder, LLMGateway
from scout_llm.prompts import PromptLibrary
from scout_llm.providers.anthropic import AnthropicProvider
from scout_llm.providers.fake import FakeProvider
from scout_llm.providers.openai import OpenAIProvider

API_KEY = "sk-test-abcdef1234567890"


async def close_gateway(gateway: LLMGateway) -> None:
    await gateway.aclose()


def warning_call(logger: Mock) -> tuple[str, str]:
    """Return the format string and its single argument from the last warning call."""
    assert logger.warning.called, "the factory must warn rather than fail silently"
    args = logger.warning.call_args.args
    assert len(args) == 2
    return str(args[0]), str(args[1])


async def test_an_empty_environment_yields_the_offline_fake_provider() -> None:
    gateway = build_gateway_from_env(environ={})

    try:
        providers = gateway.providers
        assert set(providers) == {"fake"}
        assert isinstance(providers["fake"], FakeProvider)
        assert gateway.records == ()
    finally:
        await close_gateway(gateway)


async def test_the_fake_provider_is_the_default_target_out_of_the_box() -> None:
    recorder = InMemoryRecorder()
    gateway = build_gateway_from_env(environ={}, recorder=recorder)

    try:
        assert gateway.max_concurrent_calls == 4
        assert (await gateway.usage_summary())["total_attempts"] == 0
    finally:
        await close_gateway(gateway)


async def test_a_real_provider_without_a_key_falls_back_to_fake_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = Mock()
    monkeypatch.setattr("scout_llm.factory.logger", logger)
    environ = {ENV_DEFAULT_PROVIDER: "openai", ENV_DEFAULT_MODEL: "gpt-4o"}

    gateway = build_gateway_from_env(environ=environ)

    try:
        assert set(gateway.providers) == {"fake"}
        # "Prominent" is asserted on the call, not on a rendered format: the observability
        # workstream owns rendering, and this package must warn identically whether
        # scout_observability is installed or the stdlib fallback is in use.
        message, reason = warning_call(logger)
        assert "falling back to the offline 'fake' provider" in message
        assert "no credentials present for provider 'openai'" in reason
    finally:
        await close_gateway(gateway)


async def test_an_unknown_provider_name_falls_back_to_fake_with_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = Mock()
    monkeypatch.setattr("scout_llm.factory.logger", logger)

    gateway = build_gateway_from_env(environ={ENV_DEFAULT_PROVIDER: "gemini"})

    try:
        assert set(gateway.providers) == {"fake"}
        message, reason = warning_call(logger)
        assert "falling back to the offline 'fake' provider" in message
        assert "unknown provider 'gemini'" in reason
    finally:
        await close_gateway(gateway)


async def test_a_present_openai_key_registers_the_real_provider() -> None:
    environ = {
        ENV_DEFAULT_PROVIDER: "openai",
        ENV_DEFAULT_MODEL: "gpt-4o",
        ENV_OPENAI_API_KEY: API_KEY,
    }
    gateway = build_gateway_from_env(environ=environ)

    try:
        assert set(gateway.providers) == {"fake", "openai"}
        provider = gateway.providers["openai"]
        assert isinstance(provider, OpenAIProvider)
    finally:
        await close_gateway(gateway)


async def test_a_present_anthropic_key_registers_the_real_provider() -> None:
    environ = {
        ENV_DEFAULT_PROVIDER: "anthropic",
        ENV_DEFAULT_MODEL: "claude-3-5-sonnet-20241022",
        ENV_ANTHROPIC_API_KEY: API_KEY,
    }
    gateway = build_gateway_from_env(environ=environ)

    try:
        assert set(gateway.providers) == {"fake", "anthropic"}
        assert isinstance(gateway.providers["anthropic"], AnthropicProvider)
    finally:
        await close_gateway(gateway)


async def test_both_keys_register_both_providers_and_keep_the_fake_available() -> None:
    environ = {
        ENV_OPENAI_API_KEY: API_KEY,
        ENV_ANTHROPIC_API_KEY: API_KEY,
        ENV_DEFAULT_PROVIDER: "openai",
    }
    gateway = build_gateway_from_env(environ=environ)

    try:
        assert set(gateway.providers) == {"fake", "openai", "anthropic"}
    finally:
        await close_gateway(gateway)


async def test_a_blank_provider_setting_uses_the_default() -> None:
    gateway = build_gateway_from_env(environ={ENV_DEFAULT_PROVIDER: "   "})

    try:
        assert set(gateway.providers) == {"fake"}
    finally:
        await close_gateway(gateway)


async def test_max_concurrent_calls_is_read_from_the_environment() -> None:
    gateway = build_gateway_from_env(environ={ENV_MAX_CONCURRENT_CALLS: "7"})

    try:
        assert gateway.max_concurrent_calls == 7
    finally:
        await close_gateway(gateway)


def test_a_malformed_concurrency_limit_fails_loudly() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        build_gateway_from_env(environ={ENV_MAX_CONCURRENT_CALLS: "lots"})

    assert excinfo.value.context["variable"] == ENV_MAX_CONCURRENT_CALLS


def test_a_non_positive_concurrency_limit_fails_loudly() -> None:
    with pytest.raises(ConfigurationError):
        build_gateway_from_env(environ={ENV_MAX_CONCURRENT_CALLS: "0"})


def test_a_malformed_timeout_fails_loudly() -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        build_gateway_from_env(environ={ENV_REQUEST_TIMEOUT_SECONDS: "soon"})

    assert excinfo.value.context["variable"] == ENV_REQUEST_TIMEOUT_SECONDS


def test_a_non_positive_timeout_fails_loudly() -> None:
    with pytest.raises(ConfigurationError):
        build_gateway_from_env(environ={ENV_REQUEST_TIMEOUT_SECONDS: "-5"})


async def test_an_injected_prompt_library_is_used_verbatim(tmp_path: Path) -> None:
    library = PromptLibrary(tmp_path / "custom")
    directory = library.root / AgentRole.CODE_REVIEWER.value
    directory.mkdir(parents=True)
    (directory / "v1.md").write_text("Diff:\n$diff\n", encoding="utf-8")

    gateway = build_gateway_from_env(environ={}, prompts=library)

    try:
        result = await gateway.complete(AgentRole.CODE_REVIEWER, "v1", {"diff": "+x"})
        assert result.prompt_version == "v1"
    finally:
        await close_gateway(gateway)


async def test_a_retry_policy_and_pricing_table_can_be_injected() -> None:
    policy = RetryPolicy(max_attempts=1, jitter_ratio=0.0)
    gateway = build_gateway_from_env(environ={}, retry_policy=policy, pricing={})

    try:
        assert gateway.providers
    finally:
        await close_gateway(gateway)


def test_default_prompts_root_points_at_the_repository_tree() -> None:
    """The prompts tree ships with the code, so it must resolve from the code's location.

    Deliberately does not assert the repository directory's *name*. An earlier version asserted
    ``root.parent.name == "scout_code"``, which was true only on the machine that wrote it and
    failed on every fresh clone -- a test that encodes a local path turns "works on my machine"
    into a broken handover. The property that actually matters is that the resolved directory is
    the repository root, and that is provable from the repository's own contents.
    """
    root = default_prompts_root()

    assert root.name == "prompts"
    assert root.is_dir(), f"the prompts directory does not exist at {root}"

    repository_root = root.parent
    assert (repository_root / "pyproject.toml").is_file(), (
        f"{root} is not inside a Scout checkout: no pyproject.toml in {repository_root}"
    )
    assert (repository_root / "packages" / "llm" / "scout_llm" / "factory.py").is_file()

    # And it must actually hold the per-role prompt directories the library addresses.
    for role_directory in ("repository_analyzer", "code_reviewer", "security"):
        assert (root / role_directory).is_dir(), f"missing prompt role directory: {role_directory}"


def test_environment_variable_names_match_dot_env_example() -> None:
    """These strings are the contract with ``.env.example``; a rename must be deliberate."""
    assert ENV_DEFAULT_PROVIDER == "SCOUT_LLM_DEFAULT_PROVIDER"
    assert ENV_DEFAULT_MODEL == "SCOUT_LLM_DEFAULT_MODEL"
    assert ENV_OPENAI_API_KEY == "SCOUT_OPENAI_API_KEY"
    assert ENV_ANTHROPIC_API_KEY == "SCOUT_ANTHROPIC_API_KEY"
    assert ENV_MAX_CONCURRENT_CALLS == "SCOUT_LLM_MAX_CONCURRENT_CALLS"
    assert ENV_REQUEST_TIMEOUT_SECONDS == "SCOUT_LLM_REQUEST_TIMEOUT_SECONDS"
