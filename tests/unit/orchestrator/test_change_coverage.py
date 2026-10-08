"""What Scout claims about change coverage, and what it refuses to claim.

``change_is_covered`` answers exactly one question -- "does a test execute the changed code?" --
and only a coverage measurement can answer it. These tests pin that, because the field previously
meant two different things depending on which branch produced it:

* **the census regression.** A ``coverage`` tool row is recorded even when the tool was *not
  applicable*, so the skip survives a resume. Reading that row as a measurement asserted "the change
  is exercised by tests" on the strength of a tool that never ran. The census branch then never
  executed, which is why the defect survived to the end-to-end layer.
* **the semantics the domain declares** (see ``TestOutcomeSummary``): ``True`` means a test runs
  the changed code, ``False`` means a changed production path no test touches, ``None`` means Scout
  could not determine it and deliberately does not guess.
* **the separation of concerns.** "Did this change arrive with a test edit?" is a census, not a
  coverage verdict, so it is stated as its own limitation and (when the repository asks for it)
  as its own finding -- never folded into ``change_is_covered``.
"""

from __future__ import annotations

import pytest

from scout_domain.enums import ToolStatus
from scout_domain.models.base import utcnow
from scout_domain.models.tool import ToolResult, ToolSummary
from scout_orchestrator.stages.tool_stage import (
    change_coverage_section,
    changed_source_without_test_edit,
    coverage_measurement_present,
    scan_without_test_edit_note,
    summarize_tests,
)

JOB_ID = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0"


def _result(
    tool: str,
    *,
    status: ToolStatus = ToolStatus.PASSED,
    summary: dict[str, int | float | str | bool | None] | None = None,
    exit_code: int | None = 0,
    infrastructure_failure: bool = False,
) -> ToolResult:
    """Build a tool result for one simulated run."""
    now = utcnow()
    return ToolResult(
        id=f"trun_{tool}",
        job_id=JOB_ID,
        stage="TESTING",
        tool=tool,
        command=f"{tool} -q",
        exit_code=exit_code,
        status=status,
        started_at=now,
        completed_at=now,
        duration_seconds=0.01,
        summary=ToolSummary(values=summary or {}),
        infrastructure_failure=infrastructure_failure,
    )


def _pytest(*, failed: int = 0, passed: int = 2) -> ToolResult:
    """A pytest result with the counts a real run reports."""
    return _result(
        "pytest",
        status=ToolStatus.FAILED if failed else ToolStatus.PASSED,
        exit_code=1 if failed else 0,
        summary={
            "total": failed + passed,
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "errored": 0,
            "duration_seconds": 0.02,
        },
    )


@pytest.mark.parametrize(
    ("status", "summary"),
    [
        (ToolStatus.NOT_APPLICABLE, {"not_run": True}),
        (ToolStatus.SKIPPED, {"not_run": True}),
        (ToolStatus.ERROR, {"not_run": True}),
        (ToolStatus.PASSED, {}),
    ],
)
def test_a_coverage_row_without_a_measurement_is_not_a_measurement(
    status: ToolStatus, summary: dict[str, object]
) -> None:
    """A skip, a failure, or a pass with no numbers measured nothing."""
    assert (
        coverage_measurement_present(_result("coverage", status=status, summary=summary)) is False
    )


def test_a_coverage_row_with_numbers_is_a_measurement() -> None:
    """Only a result carrying the measurement counts."""
    measured = _result("coverage", summary={"percent_covered": 91.5, "files_total": 12})
    assert coverage_measurement_present(measured) is True


def test_no_coverage_row_is_not_a_measurement() -> None:
    """The absence of a measurement is not a measurement."""
    assert coverage_measurement_present(None) is False


def test_census_branch_runs_when_only_a_skipped_coverage_row_exists() -> None:
    """The regression: a not-applicable coverage row must not suppress the census branch.

    With no measurement, the field is left undetermined rather than claimed -- and, crucially, it is
    *not* silently set to ``True`` by the skipped row.
    """
    skipped_coverage = _result(
        "coverage",
        status=ToolStatus.NOT_APPLICABLE,
        summary={"not_run": True},
        exit_code=None,
    )

    summary = summarize_tests(
        [_pytest()],
        changed_paths=["widget.py"],
        coverage_result=skipped_coverage,
    )

    assert summary.change_is_covered is None
    assert summary.uncovered_changed_paths == []
    assert summary.coverage_percent is None


def test_source_only_change_is_reported_by_the_census_not_by_the_field() -> None:
    """A source-only change is a census fact, and the field stays undetermined."""
    summary = summarize_tests(
        [_pytest(), _result("coverage", status=ToolStatus.NOT_APPLICABLE, exit_code=None)],
        changed_paths=["widget.py"],
        coverage_result=_result("coverage", status=ToolStatus.NOT_APPLICABLE, exit_code=None),
    )

    # The census answers "no test edit accompanied this change" ...
    assert changed_source_without_test_edit(["widget.py"]) == ("widget.py",)
    note = scan_without_test_edit_note(["widget.py"])
    assert note is not None
    assert "widget.py" in note
    assert "no coverage measurement was available" in note

    # ... and it does not become a coverage verdict.
    assert summary.change_is_covered is None
    section = change_coverage_section(tests=summary, changed_paths=["widget.py"])
    assert section.status.value == "WARNING"
    assert "widget.py" in section.summary


def test_a_test_edit_clears_the_census() -> None:
    """A change that modified a test is not reported as a source-only change."""
    changed = ["widget.py", "tests/test_widget.py"]

    assert changed_source_without_test_edit(changed) == ()
    assert scan_without_test_edit_note(changed) is None
    section = change_coverage_section(
        tests=summarize_tests([_pytest()], changed_paths=changed), changed_paths=changed
    )
    assert section.status.value == "INFO"
    assert "did add or modify a test" in section.summary


def test_a_measurement_sets_the_field_both_ways() -> None:
    """With a measurement the field answers its question directly, in both directions."""
    covered = summarize_tests(
        [_pytest()],
        changed_paths=["widget.py"],
        coverage_result=_result(
            "coverage",
            summary={"percent_covered": 100.0, "uncovered_changed_paths": ""},
        ),
    )
    assert covered.change_is_covered is True
    assert covered.coverage_percent == 100.0

    uncovered = summarize_tests(
        [_pytest()],
        changed_paths=["widget.py"],
        coverage_result=_result(
            "coverage",
            summary={"percent_covered": 62.5, "uncovered_changed_paths": "widget.py,helper.py"},
        ),
    )
    assert uncovered.change_is_covered is False
    assert uncovered.uncovered_changed_paths == ["widget.py", "helper.py"]
    section = change_coverage_section(tests=uncovered, changed_paths=["widget.py"])
    assert section.status.value == "WARNING"


def test_failing_tests_do_not_change_the_coverage_claim() -> None:
    """A test that fails *because of* the change still exercises the change (spec 67).

    Failing tests are reported through ``failed`` and the recommendation; coverage answers a
    different question, and conflating them would invert the field's meaning.
    """
    summary = summarize_tests(
        [_pytest(failed=1, passed=0)],
        changed_paths=["widget.py"],
        coverage_result=_result(
            "coverage", summary={"percent_covered": 100.0, "uncovered_changed_paths": ""}
        ),
    )

    assert summary.failed == 1
    assert summary.change_is_covered is True


def test_nothing_ran_leaves_the_field_undetermined() -> None:
    """No test result and no measurement means Scout says nothing at all."""
    summary = summarize_tests([], changed_paths=["widget.py"])

    assert summary.total == 0
    assert summary.change_is_covered is None


def test_a_change_without_source_has_no_census_finding() -> None:
    """A documentation-only change is not a "source without a test" change."""
    assert changed_source_without_test_edit(["README.md"]) == ()
    assert scan_without_test_edit_note(["README.md"]) is None
