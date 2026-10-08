"""The evidence model (spec section 34, section 88).

Scout's single most important architectural property is the separation between
*reasoning* and *evidence*. This module is where that separation becomes executable
rather than aspirational.

Every :class:`EvidenceReference` declares both *what kind of source* it points at
and *how strong* that source is. Strength is not a matter of opinion:

===========================  =========================================================
Strength                     Source
===========================  =========================================================
``MACHINE``                  Output of deterministic execution: a tool run, a test
                             run, a git diff, a GitHub check. Cannot be argued with.
``INSPECTION``               Direct observation of a file or repository structure,
                             pinned to a path and line range.
``AUTHORITATIVE``            A human answer or an approved engineering decision.
                             Outranks anything Scout inferred.
``INFERENCE``                An AI claim. Useful, never sufficient on its own.
===========================  =========================================================

Findings combine these; :mod:`scout_domain.models.finding` caps the confidence of a
finding whose only support is inference, and forbids such a finding from being
presented as verified.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import Field, model_validator

from scout_domain.enums import EvidenceKind, Severity
from scout_domain.models.base import ScoutModel

__all__ = [
    "CONFIDENCE_CEILING",
    "EVIDENCE_STRENGTH",
    "MAX_EXCERPT_CHARS",
    "UNSUPPORTED_CONFIDENCE_CEILING",
    "EvidenceReference",
    "EvidenceStrength",
    "evidence_strength_of",
    "strongest_evidence",
]


class EvidenceStrength(StrEnum):
    """How much a piece of evidence is entitled to be trusted."""

    MACHINE = "MACHINE"
    INSPECTION = "INSPECTION"
    AUTHORITATIVE = "AUTHORITATIVE"
    INFERENCE = "INFERENCE"

    @property
    def rank(self) -> int:
        return _STRENGTH_RANK[self]


_STRENGTH_RANK: Final[dict[EvidenceStrength, int]] = {
    EvidenceStrength.INFERENCE: 0,
    EvidenceStrength.AUTHORITATIVE: 1,
    EvidenceStrength.INSPECTION: 2,
    EvidenceStrength.MACHINE: 3,
}

#: The classification of every evidence kind. Adding a new kind without classifying it
#: is a type error at review time, and ``test_every_evidence_kind_is_classified`` fails.
EVIDENCE_STRENGTH: Final[dict[EvidenceKind, EvidenceStrength]] = {
    EvidenceKind.TOOL_RUN: EvidenceStrength.MACHINE,
    EvidenceKind.TEST_RUN: EvidenceStrength.MACHINE,
    EvidenceKind.GIT_DIFF: EvidenceStrength.MACHINE,
    EvidenceKind.GITHUB_CHECK: EvidenceStrength.MACHINE,
    EvidenceKind.ARTIFACT: EvidenceStrength.MACHINE,
    EvidenceKind.FILE_RANGE: EvidenceStrength.INSPECTION,
    EvidenceKind.REPOSITORY_INSPECTION: EvidenceStrength.INSPECTION,
    EvidenceKind.HUMAN_ANSWER: EvidenceStrength.AUTHORITATIVE,
    EvidenceKind.KNOWLEDGE_DOCUMENT: EvidenceStrength.AUTHORITATIVE,
    EvidenceKind.AGENT_RUN: EvidenceStrength.INFERENCE,
}

#: Maximum confidence a finding may claim when its strongest support is of this
#: strength. An AI-only finding can never be more than suggestive.
CONFIDENCE_CEILING: Final[dict[EvidenceStrength, float]] = {
    EvidenceStrength.MACHINE: 1.0,
    EvidenceStrength.INSPECTION: 0.95,
    EvidenceStrength.AUTHORITATIVE: 0.95,
    EvidenceStrength.INFERENCE: 0.65,
}

#: Confidence ceiling applied when a finding cites no evidence at all.
UNSUPPORTED_CONFIDENCE_CEILING: Final = 0.35

#: Excerpts in evidence are pointers, not storage. Raw output belongs in artifacts
#: (Rule 15: preserve raw evidence; spec section 21: logs are stored as artifacts).
MAX_EXCERPT_CHARS: Final = 4_000


def evidence_strength_of(kind: EvidenceKind) -> EvidenceStrength:
    """Return the strength classification for an evidence kind."""
    return EVIDENCE_STRENGTH[kind]


class EvidenceReference(ScoutModel):
    """A pointer to something Scout actually observed.

    Evidence is a *reference*, never a copy of the world: artifacts hold the bytes,
    the database holds the locator. This keeps the database small and guarantees that
    a finding can always be traced back to raw output.
    """

    kind: EvidenceKind
    #: Identifier of the referenced entity (tool run, test run, artifact, ...).
    reference_id: str = Field(min_length=1, max_length=200)
    #: One-line explanation of what this evidence shows, written for a human reader.
    summary: str = Field(min_length=1, max_length=500)

    file_path: str | None = Field(default=None, max_length=1024)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    #: Short verbatim excerpt. Long output lives in the artifact, not here.
    excerpt: str | None = Field(default=None, max_length=MAX_EXCERPT_CHARS)
    #: Artifact holding the full raw output, when one exists.
    artifact_id: str | None = None
    #: Deep link a human can follow (GitHub file view, dashboard job page, ...).
    url: str | None = None
    #: Severity carried by the underlying source, for evidence that has one.
    severity: Severity | None = None

    @property
    def strength(self) -> EvidenceStrength:
        return evidence_strength_of(self.kind)

    @property
    def is_machine_verified(self) -> bool:
        return self.strength is EvidenceStrength.MACHINE

    @model_validator(mode="after")
    def _validate_locator(self) -> EvidenceReference:
        if self.kind is EvidenceKind.FILE_RANGE:
            if not self.file_path:
                raise ValueError("FILE_RANGE evidence requires file_path")
            if self.line_start is None:
                raise ValueError("FILE_RANGE evidence requires line_start")

        if self.line_end is not None and self.line_start is not None:
            if self.line_end < self.line_start:
                raise ValueError(
                    f"line_end ({self.line_end}) precedes line_start ({self.line_start})"
                )
        if self.line_end is not None and self.line_start is None:
            raise ValueError("line_end cannot be set without line_start")
        return self

    def locator(self) -> str:
        """Render a compact locator such as ``src/auth.py:48-61``."""
        if not self.file_path:
            return f"{self.kind.value}:{self.reference_id}"
        if self.line_start is None:
            return self.file_path
        if self.line_end is None or self.line_end == self.line_start:
            return f"{self.file_path}:{self.line_start}"
        return f"{self.file_path}:{self.line_start}-{self.line_end}"


def strongest_evidence(
    references: list[EvidenceReference] | tuple[EvidenceReference, ...],
) -> EvidenceStrength | None:
    """Return the strongest strength present, or ``None`` when the list is empty."""
    if not references:
        return None
    return max((reference.strength for reference in references), key=lambda strength: strength.rank)
