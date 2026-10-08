"""Model request accounting (spec section 38).

Every model call is recorded: provider, model, prompt version, tokens, latency, status,
estimated cost. This is what makes "how much does Scout cost per verified pull request?"
(spec section 59) an answerable question rather than a guess.

Secrets are never stored: prompts are not persisted here at all, only a digest by which
two calls can be compared.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from scout_domain.enums import AgentRole
from scout_domain.models.base import ScoutModel, utcnow

__all__ = ["ModelRequestRecord", "ModelRequestStatus"]


class ModelRequestStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    REJECTED = "REJECTED"


class ModelRequestRecord(ScoutModel):
    """One attempt to obtain a completion from a model provider."""

    #: ``mreq_...``
    id: str = Field(min_length=1)
    job_id: str | None = Field(default=None, max_length=100)
    agent_run_id: str | None = Field(default=None, max_length=100)
    agent: AgentRole | None = None

    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=100)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_seconds: float = Field(default=0.0, ge=0.0)

    status: ModelRequestStatus
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)

    #: SHA-256 of the rendered prompt, so runs can be compared without storing content
    #: that could contain sensitive repository text.
    prompt_digest: str | None = Field(default=None, max_length=128)
    #: Attempt number for this logical request (retries increment it).
    attempt: int = Field(default=1, ge=1)

    error: str | None = Field(default=None, max_length=4000)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _validate(self) -> ModelRequestRecord:
        if self.status is ModelRequestStatus.SUCCEEDED and self.output_tokens == 0:
            # A succeeded call with zero output tokens is almost always a silent
            # failure somewhere upstream; surface it rather than recording a hollow win.
            raise ValueError("a SUCCEEDED model request must report output tokens")
        if self.status is not ModelRequestStatus.SUCCEEDED and not self.error:
            raise ValueError(f"a {self.status.value} model request must record an error")
        return self

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens
