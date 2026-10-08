"""Transport-agnostic value objects for the LLM gateway.

Nothing here knows about HTTP, OpenAI, or Anthropic. Providers translate between these
objects and a wire format; the gateway and the agents only ever see these types, which is
what makes ``SCOUT_LLM_DEFAULT_PROVIDER=fake`` a genuine substitute rather than a mock of
one particular vendor (Rule 2).

Token normalisation rule — read this before touching a provider
--------------------------------------------------------------

``TokenUsage.input_tokens`` is the **total** number of prompt tokens the provider billed
for, *including* any tokens served from or written to a prompt cache.

That matters because providers disagree about the field's meaning: OpenAI's
``usage.prompt_tokens`` already includes cached tokens, while Anthropic's
``usage.input_tokens`` *excludes* them (it reports ``cache_read_input_tokens`` and
``cache_creation_input_tokens`` separately). Every provider normalises to the rule above
at the boundary, so accounting is comparable across vendors and
:meth:`TokenUsage.total_tokens` never double counts.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Completion",
    "CompletionRequest",
    "Message",
    "MessageRole",
    "ModelPricing",
    "ModelTarget",
    "ResponseFormat",
    "TokenUsage",
]

#: ``"text"`` for free-form prose, ``"json"`` when the caller needs a JSON object.
type ResponseFormat = Literal["text", "json"]

#: The conversational roles every supported provider agrees on.
type MessageRole = Literal["system", "user", "assistant"]


class _ValueObject(BaseModel):
    """Frozen, ``extra="forbid"`` base for gateway value objects.

    Frozen because these cross a trust boundary: a provider that mutated the caller's
    request (or a caller that mutated a returned completion) would make the accounting
    record disagree with what was actually sent. ``extra="forbid"`` catches typo'd field
    names instead of silently ignoring them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(_ValueObject):
    """A single conversational turn, in provider-neutral form."""

    role: MessageRole
    content: str


class CompletionRequest(_ValueObject):
    """Everything needed to ask a model for a completion.

    ``metadata`` is gateway-local correlation data (job id, prompt digest, agent role).
    It is deliberately **not** transmitted to providers: vendor metadata fields differ
    per API and inventing one would break the wire contract. It exists so a recorded
    call can be tied back to the run that made it.
    """

    messages: list[Message] = Field(min_length=1)
    model: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    stop: list[str] | None = None
    response_format: ResponseFormat = "text"
    metadata: dict[str, str] = Field(default_factory=dict)


class TokenUsage(_ValueObject):
    """Token accounting for one provider call.

    ``cache_read_tokens`` and ``cache_write_tokens`` are informational: they are already
    included in ``input_tokens`` (see the module docstring).
    """

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        """Total tokens billed for this call.

        Cache tokens are *not* added: they are a subset of ``input_tokens``.
        """
        return self.input_tokens + self.output_tokens


class Completion(_ValueObject):
    """One successful provider response.

    ``raw`` retains the provider's own body (with credentials scrubbed) so an operator can
    debug a strange answer after the fact. It is never treated as evidence and never
    parsed for verification decisions.
    """

    text: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    provider: str
    model: str
    finish_reason: str | None = None
    latency_seconds: float = Field(default=0.0, ge=0.0)
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelPricing(_ValueObject):
    """Published per-million-token prices used for *estimates* only.

    Being wrong here changes a cost report, never a verification outcome: no code path
    branches on ``estimated_cost_usd``.
    """

    input_usd_per_million: float = Field(default=0.0, ge=0.0)
    output_usd_per_million: float = Field(default=0.0, ge=0.0)

    def estimate_cost(self, usage: TokenUsage) -> float:
        """Estimate the USD cost of ``usage``.

        Cache tokens are ignored because they are already inside ``input_tokens`` (see the
        module docstring) and cache pricing is provider-specific. The estimate is
        therefore a slight over- or under-statement depending on cache hit rate, which is
        acceptable for a dashboard and unacceptable for nothing else.
        """
        return (
            usage.input_tokens * self.input_usd_per_million
            + usage.output_tokens * self.output_usd_per_million
        ) / 1_000_000


class ModelTarget(_ValueObject):
    """Which provider and model a call should go to."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)

    @property
    def key(self) -> str:
        """``"<provider>/<model>"`` — the key format of the pricing table."""
        return f"{self.provider}/{self.model}"
