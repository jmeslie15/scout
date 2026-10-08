"""The acceptance-criteria gate: prose criteria are never reported as verified.

Spec section 23 exists because "the tests passed" is not the same claim as "the requested feature
works", and spec section 25 makes a criterion passable only with evidence *and* an observed
result. The domain's ``CriterionVerification`` validator enforces the second half; these tests
enforce the first half, which lives here: Scout must not manufacture evidence for a criterion out
of a test whose name happens to share a word with it.

That is not a hypothetical. A keyword overlap between a criterion and a test name is a *lead*, and
treating it as a verification would let any change pass its own stated requirements. The tests below
pin that it stays a lead.
"""

from __future__ import annotations

from scout_domain.config import ScoutRepositoryConfig
from scout_domain.enums import CriterionStatus, ToolStatus
from scout_domain.models.base import utcnow
from scout_domain.models.tool import ToolResult, ToolSummary
from scout_orchestrator.stages.feature_verification import (
    build_criteria_for,
    passing_tests_from_artifacts,
)

# Aliased because pytest would otherwise try to collect the imported ``test_runtime_available`` as a
# test in this module: its public name is deliberately ``test_*`` at the call site in the report
# builder, but that makes it a collection hazard when imported into a test module.
from scout_orchestrator.stages.feature_verification import (
    test_runtime_available as runtime_available_for,
)

JOB_ID = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0"

#: A criterion and a test name that share exactly one significant word, which is the whole basis of
#: the overlap heuristic.
CRITERION = "Adds a multiply() helper to widget.py so callers can scale values."
PASSING_TEST = "tests/test_widget::test_multiply"


def _config(*, enabled: bool = True, criteria: list[str] | None = None) -> ScoutRepositoryConfig:
    """Build a repository configuration with feature verification configured."""
    return ScoutRepositoryConfig.model_validate(
        {
            "project": {"name": "widget"},
            "feature_verification": {
                "enabled": enabled,
                "acceptance_criteria": criteria or [],
            },
        }
    )


def _pytest(
    *,
    passed: int = 1,
    failed: int = 0,
    status: ToolStatus | None = None,
    infrastructure_failure: bool = False,
) -> ToolResult:
    """Build a test-run result."""
    now = utcnow()
    resolved = status or (ToolStatus.FAILED if failed else ToolStatus.PASSED)
    return ToolResult(
        id="trun_pytest",
        job_id=JOB_ID,
        stage="TESTING",
        tool="pytest",
        command="pytest -q",
        exit_code=1 if failed else 0,
        status=resolved,
        started_at=now,
        completed_at=now,
        duration_seconds=0.01,
        summary=ToolSummary(values={"total": passed + failed, "passed": passed, "failed": failed}),
        infrastructure_failure=infrastructure_failure,
    )


def test_a_stated_criterion_is_never_passed_on_a_name_overlap() -> None:
    """The gate: a passing test whose name overlaps is a lead, not a verification."""
    criteria = build_criteria_for(
        config=_config(),
        requirement_text=CRITERION,
        changed_paths=["widget.py"],
        passing_tests=[PASSING_TEST],
        test_results=[_pytest()],
        test_runtime_available=True,
        head_sha="b" * 40,
    )

    assert len(criteria) == 1
    criterion = criteria[0]
    assert criterion.status is CriterionStatus.NOT_VERIFIED
    # The lead is recorded for the human, and it is explicitly not an established fact.
    assert PASSING_TEST in criterion.notes
    assert "overlaps this criterion" in criterion.notes
    # And no evidence was manufactured for it.
    assert criterion.evidence == []


def test_a_stated_criterion_is_blocked_when_the_runtime_ran_nothing() -> None:
    """A criterion Scout could not even attempt is BLOCKED.

    The domain then requires an open question explaining what is missing, which INVESTIGATING
    raises.
    """
    criteria = build_criteria_for(
        config=_config(),
        requirement_text=CRITERION,
        changed_paths=["widget.py"],
        passing_tests=[],
        test_results=[],
        test_runtime_available=False,
        head_sha="b" * 40,
    )

    assert criteria[0].status is CriterionStatus.BLOCKED


def test_disabled_feature_verification_produces_no_criteria() -> None:
    """A repository that switched the stage off is not assessed against criteria it did not send."""
    criteria = build_criteria_for(
        config=_config(enabled=False),
        requirement_text=CRITERION,
        changed_paths=["widget.py"],
        passing_tests=[PASSING_TEST],
        test_results=[_pytest()],
        test_runtime_available=True,
        head_sha="b" * 40,
    )

    assert criteria == ()


def test_declared_criteria_take_precedence_over_the_pull_request_body() -> None:
    """The repository's own words outrank prose extracted from a description."""
    criteria = build_criteria_for(
        config=_config(criteria=["the export endpoint returns a CSV"]),
        requirement_text=CRITERION,
        changed_paths=["widget.py"],
        passing_tests=[PASSING_TEST],
        test_results=[_pytest()],
        test_runtime_available=True,
        head_sha="b" * 40,
    )

    assert [criterion.criterion for criterion in criteria] == ["the export endpoint returns a CSV"]
    assert criteria[0].source == "acceptance_criteria"


def test_a_test_the_change_modified_can_be_verified_by_its_own_execution() -> None:
    """Criteria derived from a changed test *are* the procedure, so they may pass.

    This is the one path where ``PASSED`` is justified without a generated procedure: the criterion
    states "the tests in <path> pass", and running that suite is the procedure.
    """
    criteria = build_criteria_for(
        config=_config(),
        requirement_text="",
        changed_paths=["widget.py", "tests/test_widget.py"],
        passing_tests=[PASSING_TEST],
        test_results=[_pytest(passed=2)],
        test_runtime_available=True,
        head_sha="b" * 40,
    )

    assert criteria
    assert all(criterion.status is CriterionStatus.PASSED for criterion in criteria)
    # A passed criterion carries evidence and an observed result, as the domain demands.
    assert all(criterion.evidence for criterion in criteria)
    assert all(criterion.actual_result for criterion in criteria)
    assert all(criterion.source == "existing_tests" for criterion in criteria)


def test_a_changed_test_whose_suite_broke_blocks_instead_of_passing() -> None:
    """When the runtime produced nothing, the criterion is BLOCKED, not optimistically passed."""
    criteria = build_criteria_for(
        config=_config(),
        requirement_text="",
        changed_paths=["tests/test_widget.py"],
        passing_tests=[],
        test_results=[_pytest(status=ToolStatus.ERROR, failed=0)],
        test_runtime_available=False,
        head_sha="b" * 40,
    )

    assert criteria
    assert all(criterion.status is CriterionStatus.BLOCKED for criterion in criteria)


def test_passing_tests_come_from_the_stored_report_not_from_assertion() -> None:
    """With no readable JUnit report there are no passing tests, so none can claim a pass."""
    assert passing_tests_from_artifacts((), None) == ()  # type: ignore[arg-type]
    assert runtime_available_for([], []) is False
    assert runtime_available_for([_pytest()], []) is True
    # A run that failed for infrastructure reasons produced no result to rest a criterion on.
    assert runtime_available_for([_pytest(infrastructure_failure=True)], []) is False
