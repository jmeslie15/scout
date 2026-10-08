"""Check-run rendering and the recommendation-to-conclusion mapping (spec section 45)."""

from __future__ import annotations

from typing import Any

import pytest

from scout_domain.enums import Recommendation, RiskLevel
from scout_domain.models.report import TestOutcomeSummary, VerificationReport
from scout_github.checks import (
    CHECK_CONCLUSION_BY_RECOMMENDATION,
    CHECK_RUN_NAME,
    MAX_CHECK_SUMMARY_CHARS,
    MAX_EXECUTIVE_SUMMARY_CHARS,
    CheckRunConclusion,
    CheckRunStatus,
    build_check_run_payload,
    conclusion_for_recommendation,
    job_dashboard_url,
    render_check_summary,
    resolve_check_state,
    truncate_check_summary,
)
from scout_github.errors import GitHubError

DASHBOARD_URL = "http://localhost:5173"
JOB_ID = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0"
BASE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"


def _report(**overrides: Any) -> VerificationReport:
    data: dict[str, Any] = {
        "job_id": JOB_ID,
        "repository_full_name": "edrevel/scout-demo",
        "pull_request_number": 42,
        "commit_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "executive_summary": "Adds an acceptance-criteria checker with machine evidence.",
        "recommendation": Recommendation.PASS_WITH_WARNINGS,
        "risk_level": RiskLevel.MEDIUM,
        "tests": TestOutcomeSummary(total=12, passed=12, failed=0, skipped=0),
    }
    data.update(overrides)
    return VerificationReport.model_validate(data)


# -- Conclusion mapping --------------------------------------------------------------


@pytest.mark.parametrize(
    ("recommendation", "expected"),
    [
        (Recommendation.PASS, CheckRunConclusion.SUCCESS),
        (Recommendation.PASS_WITH_WARNINGS, CheckRunConclusion.SUCCESS),
        (Recommendation.REQUIRES_HUMAN_REVIEW, CheckRunConclusion.NEUTRAL),
        (Recommendation.REQUEST_CHANGES, CheckRunConclusion.FAILURE),
        (Recommendation.FAIL, CheckRunConclusion.FAILURE),
    ],
)
def test_recommendation_maps_to_conclusion(
    recommendation: Recommendation, expected: CheckRunConclusion
) -> None:
    assert conclusion_for_recommendation(recommendation) is expected


def test_mapping_covers_every_recommendation() -> None:
    assert set(CHECK_CONCLUSION_BY_RECOMMENDATION) == set(Recommendation)


def test_human_review_never_renders_as_failure_or_success() -> None:
    conclusion = conclusion_for_recommendation(Recommendation.REQUIRES_HUMAN_REVIEW)

    assert conclusion is not CheckRunConclusion.FAILURE
    assert conclusion is not CheckRunConclusion.SUCCESS
    assert conclusion is CheckRunConclusion.NEUTRAL


def test_unknown_recommendation_is_rejected() -> None:
    with pytest.raises(GitHubError, match="no GitHub check conclusion is defined"):
        conclusion_for_recommendation("MAYBE")  # type: ignore[arg-type]


# -- Check state ---------------------------------------------------------------------


def test_resolve_check_state_normalises_values() -> None:
    status, conclusion = resolve_check_state("completed", "success")

    assert status is CheckRunStatus.COMPLETED
    assert conclusion is CheckRunConclusion.SUCCESS


def test_completed_check_requires_a_conclusion() -> None:
    with pytest.raises(ValueError, match="requires a conclusion"):
        resolve_check_state(CheckRunStatus.COMPLETED, None)


def test_incomplete_check_must_not_carry_a_conclusion() -> None:
    with pytest.raises(ValueError, match="cannot carry a conclusion"):
        resolve_check_state(CheckRunStatus.IN_PROGRESS, CheckRunConclusion.SUCCESS)


def test_unknown_status_or_conclusion_is_rejected() -> None:
    with pytest.raises(ValueError, match="finished"):
        resolve_check_state("finished", CheckRunConclusion.SUCCESS)
    with pytest.raises(ValueError, match="pretty_good"):
        resolve_check_state(CheckRunStatus.COMPLETED, "pretty_good")


# -- Payload -------------------------------------------------------------------------


def test_completed_payload_shape() -> None:
    payload = build_check_run_payload(
        name=CHECK_RUN_NAME,
        head_sha=HEAD_SHA,
        conclusion=CheckRunConclusion.SUCCESS,
        title="Scout verified this change",
        summary="All checks passed.",
        details_url=f"{DASHBOARD_URL}/jobs/{JOB_ID}",
        external_id=JOB_ID,
    )

    assert payload == {
        "name": CHECK_RUN_NAME,
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "output": {"title": "Scout verified this change", "summary": "All checks passed."},
        "details_url": f"{DASHBOARD_URL}/jobs/{JOB_ID}",
        "external_id": JOB_ID,
    }


def test_in_progress_payload_omits_the_conclusion() -> None:
    payload = build_check_run_payload(
        name=CHECK_RUN_NAME,
        head_sha=HEAD_SHA,
        status=CheckRunStatus.IN_PROGRESS,
        title="Scout is verifying",
        summary="Running checks.",
    )

    assert payload["status"] == "in_progress"
    assert "conclusion" not in payload
    assert "details_url" not in payload
    assert "external_id" not in payload


@pytest.mark.parametrize(
    ("name", "head_sha"),
    [("", HEAD_SHA), ("   ", HEAD_SHA), (CHECK_RUN_NAME, ""), (CHECK_RUN_NAME, "  ")],
)
def test_payload_rejects_blank_name_or_sha(name: str, head_sha: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        build_check_run_payload(
            name=name,
            head_sha=head_sha,
            conclusion=CheckRunConclusion.SUCCESS,
        )


def test_payload_rejects_an_over_long_summary() -> None:
    with pytest.raises(ValueError, match="over GitHub's"):
        build_check_run_payload(
            name=CHECK_RUN_NAME,
            head_sha=HEAD_SHA,
            conclusion=CheckRunConclusion.SUCCESS,
            summary="x" * (MAX_CHECK_SUMMARY_CHARS + 1),
        )


# -- Summary rendering ---------------------------------------------------------------


def test_summary_contains_the_heading_recommendation_and_dashboard_link() -> None:
    summary = render_check_summary(_report(), DASHBOARD_URL)

    assert "## Scout Verification" in summary
    assert "**Recommendation:** PASS_WITH_WARNINGS" in summary
    assert f"{DASHBOARD_URL}/jobs/{JOB_ID}" in summary
    assert "**Risk level:** MEDIUM" in summary
    assert "Tests: 12 passed" in summary
    assert HEAD_SHA in summary


def test_summary_uses_the_reports_per_area_lines() -> None:
    summary = render_check_summary(_report(), DASHBOARD_URL)

    assert "### Results" in summary
    assert "Security checks passed" in summary
    assert "### Summary" in summary


def test_summary_lists_unverified_areas() -> None:
    report = _report(unverified_areas=["End-to-end browser flow could not be executed"])

    summary = render_check_summary(report, DASHBOARD_URL)

    assert "### Not verified" in summary
    assert "End-to-end browser flow could not be executed" in summary


def test_summary_stays_within_githubs_limit_for_a_large_report() -> None:
    report = _report(executive_summary="A very long summary. " * 300)

    summary = render_check_summary(report, DASHBOARD_URL)

    assert len(summary) <= MAX_CHECK_SUMMARY_CHARS
    assert f"{DASHBOARD_URL}/jobs/{JOB_ID}" in summary
    assert len(report.executive_summary) > MAX_EXECUTIVE_SUMMARY_CHARS


def test_summary_trailing_slash_in_dashboard_url_is_not_doubled() -> None:
    summary = render_check_summary(_report(), f"{DASHBOARD_URL}/")

    assert f"{DASHBOARD_URL}//jobs" not in summary
    assert f"{DASHBOARD_URL}/jobs/{JOB_ID}" in summary


def test_truncation_is_deterministic_and_points_at_the_dashboard() -> None:
    oversized = "x" * (MAX_CHECK_SUMMARY_CHARS + 5000)
    link = job_dashboard_url(DASHBOARD_URL, JOB_ID)

    first = truncate_check_summary(oversized, dashboard_url=link)
    second = truncate_check_summary(oversized, dashboard_url=link)

    assert first == second
    assert len(first) <= MAX_CHECK_SUMMARY_CHARS
    assert link in first
    assert "truncated at" in first


def test_short_summary_is_returned_unchanged() -> None:
    assert truncate_check_summary("short", dashboard_url="http://x") == "short"


def test_exactly_the_limit_is_not_truncated() -> None:
    at_limit = "y" * MAX_CHECK_SUMMARY_CHARS

    assert truncate_check_summary(at_limit, dashboard_url="http://x") == at_limit


# -- Dashboard URL -------------------------------------------------------------------


def test_job_dashboard_url_appends_the_job_path() -> None:
    assert job_dashboard_url("http://localhost:5173/", JOB_ID) == (
        f"http://localhost:5173/jobs/{JOB_ID}"
    )


def test_job_dashboard_url_requires_a_configured_dashboard() -> None:
    with pytest.raises(GitHubError, match="dashboard URL is not configured"):
        job_dashboard_url("   ", JOB_ID)


def test_job_dashboard_url_requires_a_job_id() -> None:
    with pytest.raises(GitHubError, match="without a job id"):
        job_dashboard_url(DASHBOARD_URL, " ")
