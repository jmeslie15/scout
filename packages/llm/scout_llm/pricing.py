"""Model price estimates for cost reporting (spec sections 38 and 59).

**These numbers are estimates, not invoices.** They exist so that
"what does Scout cost per verified pull request?" is answerable from
``ModelRequestRecord.estimated_cost_usd``. Three deliberate consequences:

* A wrong entry here can change a dashboard number. It can never change a verification
  outcome: no decision in Scout branches on cost (Rule 9).
* Unknown models fall back to :data:`ZERO_PRICING` instead of raising. A brand-new model
  must never break a verification run, and under-reporting is the fail-safe direction for a
  cost *estimate* — an alert fires, a run does not die.
* Prices are per million tokens, matching how vendors publish them, and are not
  automatically refreshed. A stale table is a known, visible limitation.

Keys are ``"<provider>/<model>"`` to match :attr:`scout_llm.types.ModelTarget.key`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from scout_llm.types import ModelPricing

__all__ = ["PRICING", "ZERO_PRICING", "price_for"]

#: The offline provider is free by construction; the entry exists so that a cost report
#: reads "0.00" rather than "unknown" for the default development configuration.
ZERO_PRICING: Final[ModelPricing] = ModelPricing(
    input_usd_per_million=0.0,
    output_usd_per_million=0.0,
)

#: Illustrative published list prices, in USD per million tokens.
PRICING: dict[str, ModelPricing] = {
    "fake/fake-deterministic": ZERO_PRICING,
    # OpenAI list prices (illustrative; verify before quoting them to anyone).
    "openai/gpt-4o": ModelPricing(input_usd_per_million=2.50, output_usd_per_million=10.00),
    "openai/gpt-4o-mini": ModelPricing(input_usd_per_million=0.15, output_usd_per_million=0.60),
    # Anthropic list prices (illustrative; verify before quoting them to anyone).
    "anthropic/claude-3-5-sonnet-20241022": ModelPricing(
        input_usd_per_million=3.00,
        output_usd_per_million=15.00,
    ),
    "anthropic/claude-3-5-haiku-20241022": ModelPricing(
        input_usd_per_million=0.80,
        output_usd_per_million=4.00,
    ),
}


def price_for(
    provider: str,
    model: str,
    *,
    table: Mapping[str, ModelPricing] | None = None,
) -> ModelPricing:
    """Return the pricing entry for ``provider``/``model``.

    Falls back to :data:`ZERO_PRICING` for an unknown model rather than raising: an
    unlisted model is a reporting gap, not a reason to fail a verification run.
    """
    source = PRICING if table is None else table
    return source.get(f"{provider}/{model}", ZERO_PRICING)
