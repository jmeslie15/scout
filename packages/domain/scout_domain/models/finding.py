"""The finding contract (spec sections 33 and 34)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from scout_domain.enums import FindingCategory, Severity
from scout_domain.models.base import ScoutModel, utcnow
from scout_domain.models.evidence import (
    CONFIDENCE_CEILING,
    UNSUPPORTED_CONFIDENCE_CEILING,
    EvidenceReference,
    EvidenceStrength,
    strongest_evidence,
)

__all__ = ["Finding", "FindingOrigin", "FindingProvenance"]


class FindingOrigin(StrEnum):
    """What produced the finding. Declared, never inferred."""

    TOOL = "TOOL"
    AGENT = "AGENT"


class FindingProvenance(StrEnum):
    """How a finding stands up against Scout's evidence hierarchy."""

    #: Produced by a deterministic tool and backed by machine output.
    DETERMINISTIC = "DETERMINISTIC"
    #: An AI conclusion supported by machine output or direct source inspection.
    AI_ASSISTED = "AI_ASSISTED"
    #: An AI conclusion resting only on other AI reasoning.
    AI_ONLY = "AI_ONLY"
    #: No evidence cited at all. Must never be presented as verified.
    UNSUPPORTED = "UNSUPPORTED"


class Finding(ScoutModel):
    """A single issue or observation produced by an agent or a deterministic tool.

    Confidence is *capped by evidence strength* at construction time. A model that
    reports ``confidence=0.99`` with no evidence does not produce a confident finding;
    it produces a clamped finding plus a ``confidence_capped`` flag, which is visible
    in the report and on dashboards. This is the mechanism that keeps spec section 3.1
    ("AI is not a source of truth") true in practice rather than by convention.
    """

    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    severity: Severity
    category: FindingCategory
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)

    #: ``owner/name``. Kept denormalised so a finding is readable on its own.
    repository: str = Field(min_length=1)
    file: str | None = Field(default=None, max_length=1024)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    evidence: list[EvidenceReference] = Field(default_factory=list)
    recommendation: str = Field(default="", max_length=2000)
    #: 0.0-1.0. Clamped down to the ceiling implied by the strongest evidence.
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    requires_human: bool = False

    origin: FindingOrigin
    #: The agent role or tool name that produced this finding, e.g. ``security`` or ``pytest``.
    source: str = Field(min_length=1, max_length=100)
    #: Identifier of the agent run or tool run that produced it.
    source_run_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    #: Set by the validator when confidence had to be reduced to stay within the
    #: ceiling implied by the evidence. Surfaced in the report rather than hidden.
    confidence_capped: bool = False

    @model_validator(mode="after")
    def _enforce_evidence_discipline(self) -> Finding:
        strength = strongest_evidence(self.evidence)

        if self.file is None:
            if self.line_start is not None or self.line_end is not None:
                raise ValueError("line numbers cannot be set without a file")
        elif self.line_end is not None and self.line_start is None:
            raise ValueError("line_end cannot be set without line_start")
        elif (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError(f"line_end ({self.line_end}) precedes line_start ({self.line_start})")

        if strength is None:
            # No evidence at all: cap hard and force human attention.
            claimed = self.confidence
            object.__setattr__(self, "confidence", min(claimed, UNSUPPORTED_CONFIDENCE_CEILING))
            object.__setattr__(self, "requires_human", True)
            if claimed > UNSUPPORTED_CONFIDENCE_CEILING:
                object.__setattr__(self, "confidence_capped", True)
        else:
            ceiling = CONFIDENCE_CEILING[strength]
            if self.confidence > ceiling:
                object.__setattr__(self, "confidence", ceiling)
                object.__setattr__(self, "confidence_capped", True)

        return self

    @property
    def provenance(self) -> FindingProvenance:
        """Classify this finding against Scout's evidence hierarchy."""
        strength = strongest_evidence(self.evidence)
        if strength is None:
            return FindingProvenance.UNSUPPORTED
        if self.origin is FindingOrigin.TOOL:
            return FindingProvenance.DETERMINISTIC
        if strength is EvidenceStrength.INFERENCE:
            return FindingProvenance.AI_ONLY
        return FindingProvenance.AI_ASSISTED

    @property
    def is_machine_verified(self) -> bool:
        """``True`` only when deterministic execution backs this finding."""
        return any(reference.is_machine_verified for reference in self.evidence)

    @property
    def is_verified_fact(self) -> bool:
        """Whether the report may state this as fact rather than as a concern."""
        return (
            self.provenance is FindingProvenance.DETERMINISTIC
            and self.is_machine_verified
            and not self.confidence_capped
        )

    def locator(self) -> str:
        """Render a compact human locator, e.g. ``src/auth.py:48-61``."""
        if not self.file:
            return self.repository
        if self.line_start is None:
            return self.file
        if self.line_end is None or self.line_end == self.line_start:
            return f"{self.file}:{self.line_start}"
        return f"{self.file}:{self.line_start}-{self.line_end}"
