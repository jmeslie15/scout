"""The persisted result of PREPARING, and how the other stages get it back.

Rule 5 ("resumable; no in-memory-only state") is the whole reason this module exists. When
a worker dies halfway through TESTING, the orchestrator reclaims the lease and resumes at the
first unresolved stage -- which is not PREPARING. Everything PREPARING established (the
pinned change set, the repository summary, the risk assessment, the repository
configuration) therefore has to be re-readable from durable storage, or the resume would
have to re-do the checkout and the analysis.

The document is stored as a ``REPOSITORY_SUMMARY`` artifact and is deliberately
self-contained: a single read gives the orchestrator the complete preparation state, and the
artifact's digest proves the bytes it read are the bytes that were written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from scout_domain.config import ScoutRepositoryConfig
from scout_domain.enums import RiskLevel
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.repository import ChangeSet, RepositorySummary
from scout_domain.risk import RiskAssessment, RiskSignal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from scout_domain.models.artifact import Artifact

__all__ = ["PreparedState", "find_artifact"]

#: Bumped when the document shape changes incompatibly. A stored document with a different
#: version is treated as unreadable, which fails the job loudly instead of resuming with
#: silently missing inputs.
DOCUMENT_VERSION = 1


@dataclass(frozen=True, slots=True)
class PreparedState:
    """Everything PREPARING established, in one value."""

    change_set: ChangeSet
    repository_summary: RepositorySummary
    risk: RiskAssessment
    repository_config: ScoutRepositoryConfig
    #: The raw ``.scout/engineering.yaml`` text as it existed at the verified commit. Kept
    #: verbatim because it is evidence: what Scout was asked to do is part of the record.
    config_raw: str
    pr_body: str
    requirement_text: str
    commit_messages: tuple[str, ...]

    def to_document(self) -> dict[str, Any]:
        """Serialise to a JSON-ready mapping for the artifact store."""
        return {
            "version": DOCUMENT_VERSION,
            "change_set": self.change_set.model_dump(mode="json"),
            "repository_summary": self.repository_summary.model_dump(mode="json"),
            "risk": _risk_to_document(self.risk),
            "repository_config": self.repository_config.model_dump(mode="json"),
            "config_raw": self.config_raw,
            "pr_body": self.pr_body,
            "requirement_text": self.requirement_text,
            "commit_messages": list(self.commit_messages),
        }

    def to_text(self) -> str:
        """Render the document as the bytes stored in the artifact."""
        return json.dumps(self.to_document(), indent=2, sort_keys=True)

    @classmethod
    def from_text(cls, text: str) -> PreparedState:
        """Rebuild the state from stored artifact bytes.

        Raises:
            ValueError: when the document is unreadable or written by an incompatible
                version. The caller turns this into a job failure rather than resuming with
                partially reconstructed inputs.
        """
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"prepared-state artifact is not valid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise TypeError("prepared-state artifact is not a JSON object")
        version = document.get("version")
        if version != DOCUMENT_VERSION:
            raise ValueError(
                f"prepared-state artifact has version {version!r}, expected "
                f"{DOCUMENT_VERSION}; the job cannot be resumed safely"
            )
        return cls(
            change_set=ChangeSet.model_validate(document["change_set"]),
            repository_summary=RepositorySummary.model_validate(document["repository_summary"]),
            risk=_risk_from_document(document["risk"]),
            repository_config=ScoutRepositoryConfig.model_validate(document["repository_config"]),
            config_raw=str(document.get("config_raw") or ""),
            pr_body=str(document.get("pr_body") or ""),
            requirement_text=str(document.get("requirement_text") or ""),
            commit_messages=tuple(str(item) for item in document.get("commit_messages") or []),
        )


def _risk_to_document(risk: RiskAssessment) -> dict[str, Any]:
    """Serialise a risk assessment, which is a dataclass rather than a Pydantic model."""
    return {
        "level": risk.level.value,
        "default_risk": risk.default_risk.value,
        "signals": [
            {
                "rule": signal.rule,
                "risk": signal.risk.value,
                "reason": signal.reason,
                "path": signal.path,
            }
            for signal in risk.signals
        ],
    }


def _risk_from_document(document: Mapping[str, Any]) -> RiskAssessment:
    """Rebuild a risk assessment from its stored form."""
    signals = tuple(
        RiskSignal(
            rule=str(entry["rule"]),
            risk=RiskLevel(str(entry["risk"])),
            reason=str(entry["reason"]),
            path=None if entry.get("path") is None else str(entry["path"]),
        )
        for entry in document.get("signals") or ()
    )
    return RiskAssessment(
        level=RiskLevel(str(document["level"])),
        signals=signals,
        default_risk=RiskLevel(str(document.get("default_risk", RiskLevel.MEDIUM.value))),
    )


def find_artifact(
    artifacts: Sequence[Artifact],
    *,
    kind: ArtifactKind,
    filename: str,
) -> Artifact | None:
    """Return the newest artifact of ``kind`` with ``filename``, or ``None``.

    Selection is by filename as well as kind because a job accumulates several artifacts of the
    same kind (every preparation writes a ``REPOSITORY_SUMMARY``), and picking the wrong one
    would resume a job with another document's contents. The input is expected newest-first, the
    order the artifact repository returns rows in.
    """
    return next(
        (
            artifact
            for artifact in artifacts
            if artifact.kind is kind and artifact.filename == filename
        ),
        None,
    )
