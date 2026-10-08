"""Pricing math and the unknown-model fallback (spec sections 38, 59)."""

from __future__ import annotations

import pytest

from scout_llm.pricing import PRICING, ZERO_PRICING, price_for
from scout_llm.types import ModelPricing, TokenUsage


def test_estimate_cost_uses_published_per_million_rates() -> None:
    pricing = ModelPricing(input_usd_per_million=2.50, output_usd_per_million=10.00)
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert pricing.estimate_cost(usage) == pytest.approx(12.50)


def test_estimate_cost_scales_linearly() -> None:
    pricing = ModelPricing(input_usd_per_million=3.00, output_usd_per_million=15.00)
    usage = TokenUsage(input_tokens=2_000, output_tokens=500)
    expected = (2_000 * 3.00 + 500 * 15.00) / 1_000_000
    assert pricing.estimate_cost(usage) == pytest.approx(expected)


def test_estimate_cost_ignores_cache_tokens() -> None:
    """Cache tokens are a subset of input_tokens, so adding them would double count."""
    pricing = ModelPricing(input_usd_per_million=2.50, output_usd_per_million=10.00)
    plain = TokenUsage(input_tokens=1_000, output_tokens=100)
    cached = TokenUsage(
        input_tokens=1_000,
        output_tokens=100,
        cache_read_tokens=800,
        cache_write_tokens=50,
    )
    assert pricing.estimate_cost(cached) == pytest.approx(pricing.estimate_cost(plain))


def test_zero_pricing_costs_nothing() -> None:
    assert ZERO_PRICING.estimate_cost(TokenUsage(input_tokens=10**9, output_tokens=10**9)) == 0.0


def test_price_for_known_model_returns_table_entry() -> None:
    entry = price_for("openai", "gpt-4o")
    assert entry is PRICING["openai/gpt-4o"]
    assert entry.input_usd_per_million > 0.0


def test_price_for_unknown_model_falls_back_to_zero_without_raising() -> None:
    """A brand-new model must never break a verification run — only under-report cost."""
    assert price_for("openai", "gpt-99-turbo-unreleased") is ZERO_PRICING
    assert price_for("no-such-provider", "no-such-model").estimate_cost(TokenUsage()) == 0.0


def test_pricing_table_has_a_fake_entry() -> None:
    assert "fake/fake-deterministic" in PRICING
    assert PRICING["fake/fake-deterministic"] is ZERO_PRICING


def test_price_for_accepts_an_injected_table() -> None:
    custom = {"fake/special": ModelPricing(input_usd_per_million=1.0, output_usd_per_million=1.0)}
    assert price_for("fake", "special", table=custom).input_usd_per_million == 1.0
    # An injected table replaces the default rather than merging with it.
    assert price_for("openai", "gpt-4o", table=custom) is ZERO_PRICING


def test_pricing_rejects_negative_rates() -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError is a ValueError
        ModelPricing(input_usd_per_million=-1.0, output_usd_per_million=1.0)
