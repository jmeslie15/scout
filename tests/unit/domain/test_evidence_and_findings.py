"""Tests for the evidence hierarchy and the finding contract.

These are the tests that enforce spec section 3.1 ("AI is not a source of truth") and
section 34 ("AI-generated claims without evidence should have lower confidence and should
never be presented as verified facts"). If someone weakens that property, these fail.
"""

from __future__ import annotations

import pytest

from scout_domain.enums import EvidenceKind, FindingCategory, Severity
from scout_domain.models.evidence import (
    CONFIDENCE_CEILING,
    EVIDENCE_STRENGTH,
    UNSUPPORTED_CONFIDENCE_CEILING,
    EvidenceReference,
    EvidenceStrength,
    evidence_strength_of,
    strongest_evidence,
)
from scout_domain.models.finding import Finding, FindingOrigin, FindingProvenance


def tool_evidence() -> EvidenceReference:
    return EvidenceReference(
        kind=EvidenceKind.TOOL_RUN,
        reference_id="trun_1",
        summary="pytest reported 3 failures",
        artifact_id="art_1",
    )


def inspection_evidence() -> EvidenceReference:
    return EvidenceReference(
        kind=EvidenceKind.FILE_RANGE,
        reference_id="find_1",
        summary="the branch returns before validating the token",
        file_path="src/auth.py",
        line_start=48,
        line_end=61,
    )


def inference_evidence() -> EvidenceReference:
    return EvidenceReference(
        kind=EvidenceKind.AGENT_RUN, reference_id="arun_1", summary="model opinion"
    )


def make_finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "id": "find_1",
        "job_id": "job_1",
        "severity": Severity.MEDIUM,
        "category": FindingCategory.CORRECTNESS,
        "title": "Possible missing authorization check",
        "description": "The handler may not verify the caller's role.",
        "repository": "edrevel/scout",
        "origin": FindingOrigin.AGENT,
        "source": "code_reviewer",
        "confidence": 0.9,
    }
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


class TestEvidenceStrengthIsTotal:
    def test_every_evidence_kind_is_classified(self) -> None:
        """Adding a kind without classifying it must fail here, not silently default."""
        assert set(EVIDENCE_STRENGTH) == set(EvidenceKind)

    def test_every_strength_has_a_confidence_ceiling(self) -> None:
        assert set(CONFIDENCE_CEILING) == set(EvidenceStrength)

    def test_classification_is_sensible(self) -> None:
        assert evidence_strength_of(EvidenceKind.TOOL_RUN) is EvidenceStrength.MACHINE
        assert evidence_strength_of(EvidenceKind.TEST_RUN) is EvidenceStrength.MACHINE
        assert evidence_strength_of(EvidenceKind.FILE_RANGE) is EvidenceStrength.INSPECTION
        assert evidence_strength_of(EvidenceKind.HUMAN_ANSWER) is EvidenceStrength.AUTHORITATIVE
        assert evidence_strength_of(EvidenceKind.AGENT_RUN) is EvidenceStrength.INFERENCE

    def test_machine_outranks_everything(self) -> None:
        assert EvidenceStrength.MACHINE.rank > EvidenceStrength.INSPECTION.rank
        assert EvidenceStrength.INSPECTION.rank > EvidenceStrength.AUTHORITATIVE.rank
        assert EvidenceStrength.AUTHORITATIVE.rank > EvidenceStrength.INFERENCE.rank

    def test_strongest_evidence_picks_the_maximum(self) -> None:
        assert (
            strongest_evidence([inference_evidence(), tool_evidence(), inspection_evidence()])
            is EvidenceStrength.MACHINE
        )
        assert strongest_evidence([]) is None


class TestEvidenceReferenceValidation:
    def test_file_range_requires_a_path_and_line(self) -> None:
        with pytest.raises(ValueError, match="file_path"):
            EvidenceReference(kind=EvidenceKind.FILE_RANGE, reference_id="x", summary="s")
        with pytest.raises(ValueError, match="line_start"):
            EvidenceReference(
                kind=EvidenceKind.FILE_RANGE, reference_id="x", summary="s", file_path="a.py"
            )

    def test_line_end_cannot_precede_line_start(self) -> None:
        with pytest.raises(ValueError, match="precedes"):
            EvidenceReference(
                kind=EvidenceKind.FILE_RANGE,
                reference_id="x",
                summary="s",
                file_path="a.py",
                line_start=10,
                line_end=5,
            )

    def test_locator_rendering(self) -> None:
        assert inspection_evidence().locator() == "src/auth.py:48-61"
        assert tool_evidence().locator() == "tool_run:trun_1"

    def test_excerpt_is_bounded(self) -> None:
        with pytest.raises(ValueError, match=r"at most|string_too_long|excerpt"):
            EvidenceReference(
                kind=EvidenceKind.TOOL_RUN,
                reference_id="x",
                summary="s",
                excerpt="a" * 5000,
            )


class TestConfidenceCapping:
    def test_ai_only_finding_is_capped(self) -> None:
        finding = make_finding(evidence=[inference_evidence()], confidence=0.99)
        assert finding.confidence == CONFIDENCE_CEILING[EvidenceStrength.INFERENCE]
        assert finding.confidence_capped is True

    def test_inspection_backed_finding_has_a_higher_ceiling(self) -> None:
        finding = make_finding(evidence=[inspection_evidence()], confidence=0.99)
        assert finding.confidence == CONFIDENCE_CEILING[EvidenceStrength.INSPECTION]

    def test_machine_backed_finding_may_be_certain(self) -> None:
        finding = make_finding(evidence=[tool_evidence()], confidence=0.99)
        assert finding.confidence == 0.99
        assert finding.confidence_capped is False

    def test_unsupported_finding_is_capped_hard_and_flagged_for_humans(self) -> None:
        finding = make_finding(evidence=[], confidence=0.95)
        assert finding.confidence == UNSUPPORTED_CONFIDENCE_CEILING
        assert finding.confidence_capped is True
        assert finding.requires_human is True

    def test_confidence_below_the_ceiling_is_left_alone(self) -> None:
        finding = make_finding(evidence=[inference_evidence()], confidence=0.2)
        assert finding.confidence == 0.2
        assert finding.confidence_capped is False


class TestProvenance:
    def test_tool_origin_is_deterministic(self) -> None:
        finding = make_finding(origin=FindingOrigin.TOOL, evidence=[tool_evidence()])
        assert finding.provenance is FindingProvenance.DETERMINISTIC
        assert finding.is_verified_fact is True

    def test_agent_with_machine_evidence_is_ai_assisted(self) -> None:
        finding = make_finding(origin=FindingOrigin.AGENT, evidence=[tool_evidence()])
        assert finding.provenance is FindingProvenance.AI_ASSISTED
        assert finding.is_verified_fact is False

    def test_agent_with_only_inference_is_ai_only(self) -> None:
        finding = make_finding(origin=FindingOrigin.AGENT, evidence=[inference_evidence()])
        assert finding.provenance is FindingProvenance.AI_ONLY
        assert finding.is_verified_fact is False

    def test_no_evidence_is_unsupported(self) -> None:
        finding = make_finding(evidence=[])
        assert finding.provenance is FindingProvenance.UNSUPPORTED
        assert finding.is_verified_fact is False

    def test_a_tool_finding_without_machine_evidence_is_not_a_verified_fact(self) -> None:
        """Declaring origin=TOOL is not sufficient; the evidence must be machine output."""
        finding = make_finding(origin=FindingOrigin.TOOL, evidence=[inference_evidence()])
        assert finding.is_verified_fact is False


class TestFindingValidation:
    def test_line_numbers_require_a_file(self) -> None:
        with pytest.raises(ValueError, match="without a file"):
            make_finding(line_start=10)

    def test_line_end_requires_line_start(self) -> None:
        with pytest.raises(ValueError, match="line_end"):
            make_finding(file="a.py", line_end=10)

    def test_locator_rendering(self) -> None:
        assert make_finding(file="src/auth.py", line_start=4, line_end=6).locator() == (
            "src/auth.py:4-6"
        )
        assert make_finding(file="src/auth.py", line_start=4).locator() == "src/auth.py:4"
        assert make_finding(file="src/auth.py").locator() == "src/auth.py"
        assert make_finding().locator() == "edrevel/scout"

    def test_extra_fields_are_rejected(self) -> None:
        """Contract strictness: an agent inventing a field must fail loudly."""
        with pytest.raises(ValueError, match=r"unexpected_field|extra_forbidden|Extra inputs"):
            make_finding(unexpected_field="value")
