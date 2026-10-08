"""Risk-based skipping, and the operator floor that a repository cannot override (spec section 41).

Spec section 41 lets a LOW-risk change skip ``SECURITY_SCANNING``. The security property that makes
that safe is the *order* of the checks in
:func:`scout_orchestrator.state.skip_decision`: ``SCOUT_REQUIRED_STAGES`` is consulted before the
repository's own ``disabled_stages``, so a pull request cannot edit ``.scout/engineering.yaml`` to
switch off the check that would have caught it.

These tests also pin the stages that are never skippable at all: PREPARING (nothing downstream has a
checkout without it) and INVESTIGATING (a repository could otherwise silence every question its own
change raised).
"""

from __future__ import annotations

import pytest

from scout_domain.config import ScoutRepositoryConfig
from scout_domain.enums import JobStatus, RiskLevel
from scout_domain.risk import RiskAssessment, gate_for_risk
from scout_orchestrator.state import NEVER_SKIPPABLE, skip_decision


def _config(
    *,
    disabled: list[str] | None = None,
    required: list[str] | None = None,
    security_enabled: bool = True,
) -> ScoutRepositoryConfig:
    """Build a repository configuration with stage overrides."""
    return ScoutRepositoryConfig.model_validate(
        {
            "project": {"name": "widget"},
            "verification": {
                "disabled_stages": disabled or [],
                "required_stages": required or [],
            },
            "security": {"enabled": security_enabled},
        }
    )


def _risk(level: RiskLevel) -> RiskAssessment:
    """Build a risk assessment at a level."""
    return RiskAssessment(level=level)


def test_medium_risk_always_runs_security_scanning() -> None:
    """The risk gate for MEDIUM requires human review, so no automated skip is permitted."""
    decision = skip_decision(
        JobStatus.SECURITY_SCANNING,
        config=_config(),
        risk=_risk(RiskLevel.MEDIUM),
        required_stages=frozenset(),
    )

    assert decision.runs
    assert decision.reason is None


def test_low_risk_may_skip_security_scanning_with_a_reason() -> None:
    """The one stage the risk gate permits skipping, and it must say why."""
    assert not gate_for_risk(RiskLevel.LOW).requires_human_review

    decision = skip_decision(
        JobStatus.SECURITY_SCANNING,
        config=_config(),
        risk=_risk(RiskLevel.LOW),
        required_stages=frozenset(),
    )

    assert decision.skip
    assert decision.reason is not None
    assert "LOW" in decision.reason


def test_a_repository_may_disable_a_stage_it_is_allowed_to_disable() -> None:
    """``disabled_stages`` is honoured, and the skip carries the configuration as its reason."""
    decision = skip_decision(
        JobStatus.SECURITY_SCANNING,
        config=_config(disabled=["SECURITY_SCANNING"]),
        risk=_risk(RiskLevel.LOW),
        required_stages=frozenset(),
    )

    assert decision.skip
    assert "disabled_stages" in (decision.reason or "")


def test_operator_floor_beats_the_repository_disabled_list() -> None:
    """A repository must not be able to switch off a stage the operator requires."""
    decision = skip_decision(
        JobStatus.SECURITY_SCANNING,
        config=_config(disabled=["SECURITY_SCANNING"]),
        risk=_risk(RiskLevel.LOW),
        required_stages=frozenset({JobStatus.SECURITY_SCANNING}),
    )

    assert decision.runs
    assert decision.reason is None


def test_operator_floor_also_beats_low_risk() -> None:
    """The floor is a floor: LOW risk does not license skipping a required stage either."""
    decision = skip_decision(
        JobStatus.SECURITY_SCANNING,
        config=_config(),
        risk=_risk(RiskLevel.LOW),
        required_stages=frozenset({JobStatus.SECURITY_SCANNING}),
    )

    assert decision.runs


def test_repository_may_require_a_stage_for_itself() -> None:
    """A repository's own required list is honoured before its disabled list was even consulted."""
    decision = skip_decision(
        JobStatus.SECURITY_SCANNING,
        config=_config(required=["SECURITY_SCANNING"]),
        risk=_risk(RiskLevel.LOW),
        required_stages=frozenset(),
    )

    assert decision.runs


def test_security_disabled_in_repository_configuration() -> None:
    """``security.enabled: false`` is honoured, and named in the reason."""
    decision = skip_decision(
        JobStatus.SECURITY_SCANNING,
        config=_config(security_enabled=False),
        risk=_risk(RiskLevel.MEDIUM),
        required_stages=frozenset(),
    )

    assert decision.skip
    assert "security.enabled" in (decision.reason or "")


def test_structural_stages_are_never_skippable() -> None:
    """PREPARING and INVESTIGATING cannot be disabled, however the repository is configured."""
    assert frozenset({JobStatus.PREPARING, JobStatus.INVESTIGATING}) == NEVER_SKIPPABLE

    for stage in NEVER_SKIPPABLE:
        decision = skip_decision(
            stage,
            config=_config(disabled=[stage.value]),
            risk=_risk(RiskLevel.LOW),
            required_stages=frozenset(),
        )
        assert decision.runs, stage


@pytest.mark.parametrize(
    "stage",
    [
        JobStatus.ANALYZING,
        JobStatus.REVIEWING,
        JobStatus.TESTING,
        JobStatus.FEATURE_VERIFYING,
    ],
)
def test_other_stages_always_run(stage: JobStatus) -> None:
    """Only the security scan is risk-skippable: no change can skip the test suite."""
    decision = skip_decision(
        stage, config=_config(), risk=_risk(RiskLevel.LOW), required_stages=frozenset()
    )

    assert decision.runs


def test_unknown_stage_names_are_ignored_not_fatal() -> None:
    """A typo in a stage name protects nothing, and is reported during PREPARING instead."""
    decision = skip_decision(
        JobStatus.SECURITY_SCANNING,
        config=_config(disabled=["SECURTY_SCANNING"]),
        risk=_risk(RiskLevel.MEDIUM),
        required_stages=frozenset(),
    )

    assert decision.runs
