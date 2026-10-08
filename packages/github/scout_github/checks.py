"""Check-run rendering (spec section 45).

Scout's only GitHub write is a check run. That makes this module the entire interface
between Scout's conclusion and the engineer looking at a pull request, so two rules are
enforced here rather than left to callers:

* **The conclusion mapping is fixed in one place.** ``REQUIRES_HUMAN_REVIEW`` maps to
  ``neutral``; see :func:`conclusion_for_recommendation` for why that specific mapping
  is load-bearing.
* **The summary is truncated deterministically.** GitHub rejects a check run whose
  ``output.summary`` exceeds 65 535 characters, and it rejects it with an error that
  gives no hint about length. A truncation that points at the dashboard keeps the PR
  useful; a failed check run would hide the verification entirely.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from scout_domain.enums import Recommendation
from scout_domain.models.report import VerificationReport
from scout_github.errors import GitHubError

__all__ = [
    "CHECK_CONCLUSION_BY_RECOMMENDATION",
    "CHECK_RUN_NAME",
    "MAX_CHECK_SUMMARY_CHARS",
    "MAX_EXECUTIVE_SUMMARY_CHARS",
    "CheckRunConclusion",
    "CheckRunStatus",
    "build_check_run_payload",
    "conclusion_for_recommendation",
    "job_dashboard_url",
    "render_check_summary",
    "resolve_check_state",
    "truncate_check_summary",
]

#: The check-run name shown on the PR. Stable on purpose: renaming it would break
#: branch-protection rules that require this check.
CHECK_RUN_NAME: Final = "Scout Verification"

#: GitHub's hard limit on ``output.summary``.
MAX_CHECK_SUMMARY_CHARS: Final = 65_535

#: How much of the report's executive summary is echoed into the check. The check exists
#: to answer "what did Scout conclude?" at a glance; the whole report belongs on the
#: dashboard, which is linked from the same block.
MAX_EXECUTIVE_SUMMARY_CHARS: Final = 2000

#: Dashboard path for one job. ``dashboard_url`` is the dashboard root.
DASHBOARD_JOB_PATH_TEMPLATE: Final = "/jobs/{job_id}"

_TRUNCATION_NOTICE_TEMPLATE: Final = (
    "\n\n---\n\n_This summary was truncated at {limit} characters. "
    "The complete verification report is on the "
    "[Scout dashboard]({dashboard_url})._\n"
)


class CheckRunStatus(StrEnum):
    """GitHub check-run statuses Scout uses (spec section 45)."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class CheckRunConclusion(StrEnum):
    """GitHub check-run conclusions.

    ``neutral`` carries real meaning here rather than being a fallback: it is the only
    conclusion that says "Scout is not judging this" without either passing or failing
    the change.
    """

    ACTION_REQUIRED = "action_required"
    CANCELLED = "cancelled"
    FAILURE = "failure"
    NEUTRAL = "neutral"
    NOTICE = "notice"
    SKIPPED = "skipped"
    STALE = "stale"
    SUCCESS = "success"
    TIMED_OUT = "timed_out"


#: Scout recommendation to GitHub conclusion. See
#: :func:`conclusion_for_recommendation` for the reasoning behind each entry.
CHECK_CONCLUSION_BY_RECOMMENDATION: Final[Mapping[Recommendation, CheckRunConclusion]] = {
    Recommendation.PASS: CheckRunConclusion.SUCCESS,
    Recommendation.PASS_WITH_WARNINGS: CheckRunConclusion.SUCCESS,
    Recommendation.REQUIRES_HUMAN_REVIEW: CheckRunConclusion.NEUTRAL,
    Recommendation.REQUEST_CHANGES: CheckRunConclusion.FAILURE,
    Recommendation.FAIL: CheckRunConclusion.FAILURE,
}


def conclusion_for_recommendation(recommendation: Recommendation) -> CheckRunConclusion:
    """Map a Scout recommendation onto the GitHub check conclusion.

    Why ``REQUIRES_HUMAN_REVIEW`` is ``neutral`` and must stay that way: it is the only
    outcome that means "Scout has not decided". Rendering it as ``failure`` would mark a
    change as rejected when Scout has not judged it — a human-review request is not a
    verdict, and a red check blocks merges under branch protection. Rendering it as
    ``success`` would let an unverified change look verified, which is the exact
    overstatement spec section 2 forbids. ``neutral`` is GitHub's no-judgement
    conclusion, so it is the only honest mapping.

    ``PASS_WITH_WARNINGS`` maps to ``success`` because the report's own gates guarantee
    that warnings only accompany findings Scout judged non-blocking; the warnings are
    visible in the check summary either way.
    """
    try:
        return CHECK_CONCLUSION_BY_RECOMMENDATION[recommendation]
    except KeyError as exc:  # pragma: no cover - Recommendation is a closed enum
        raise GitHubError(
            f"no GitHub check conclusion is defined for recommendation {recommendation!r}",
            recommendation=str(recommendation),
        ) from exc


def resolve_check_state(
    status: CheckRunStatus | str,
    conclusion: CheckRunConclusion | str | None,
) -> tuple[CheckRunStatus, CheckRunConclusion | None]:
    """Validate and normalise a check-run status/conclusion pair.

    GitHub rejects both invalid combinations and invalid *values* — a conclusion that is
    not one of its nine literals, or a conclusion on a check run that is not completed.
    Validating here turns a remote 422 with an opaque body into a local, immediate error
    naming the offending value.

    Args:
        status: ``queued``, ``in_progress``, or ``completed``.
        conclusion: One of GitHub's conclusions, or ``None`` while the run is
            incomplete.

    Returns:
        The normalised ``(status, conclusion)`` pair.

    Raises:
        ValueError: When the status is unknown, a conclusion is required but missing, or
            a conclusion is supplied for a check run that has not completed.
    """
    resolved_status = CheckRunStatus(status)
    resolved_conclusion = None if conclusion is None else CheckRunConclusion(conclusion)

    if resolved_status is CheckRunStatus.COMPLETED and resolved_conclusion is None:
        raise ValueError(
            "a completed check run requires a conclusion (GitHub rejects a completed "
            "check run without one)"
        )
    if resolved_status is not CheckRunStatus.COMPLETED and resolved_conclusion is not None:
        raise ValueError(
            f"a {resolved_status.value} check run cannot carry a conclusion "
            f"({resolved_conclusion.value}); GitHub only accepts conclusions on "
            "completed check runs"
        )
    return resolved_status, resolved_conclusion


def build_check_run_payload(
    *,
    name: str,
    head_sha: str,
    status: CheckRunStatus | str = CheckRunStatus.COMPLETED,
    conclusion: CheckRunConclusion | str | None = None,
    title: str = "",
    summary: str = "",
    details_url: str | None = None,
    external_id: str | None = None,
) -> dict[str, Any]:
    """Build the JSON body for ``POST /repos/{owner}/{repo}/check-runs``.

    Kept separate from the HTTP client so the exact request body is unit-testable
    without a transport, and so :class:`~scout_github.client.FakeGitHubClient` validates
    payloads exactly as the real API would.

    Args:
        name: Check-run name; conventionally :data:`CHECK_RUN_NAME`.
        head_sha: The commit the check applies to. Scout always passes a pinned SHA.
        status: Check-run status.
        conclusion: Required once ``status`` is ``completed``.
        title: Short heading rendered inside the check.
        summary: Markdown summary; run it through :func:`truncate_check_summary` first
            when it contains report text.
        details_url: Where the full report lives (the Scout dashboard job page).
        external_id: Scout's own job id, so a check run can be mapped back to a job.

    Returns:
        A JSON-serialisable request body.

    Raises:
        ValueError: When ``name``/``head_sha`` is empty, or the status/conclusion pair is
            one GitHub rejects.
    """
    if not name.strip():
        raise ValueError("check run name must not be empty")
    if not head_sha.strip():
        raise ValueError(
            "check run head_sha must not be empty: a check run that is not pinned to a "
            "commit cannot be evidence (spec section 13)"
        )
    if len(summary) > MAX_CHECK_SUMMARY_CHARS:
        raise ValueError(
            f"check run summary is {len(summary)} characters, over GitHub's "
            f"{MAX_CHECK_SUMMARY_CHARS}-character limit; truncate it with "
            "truncate_check_summary() first"
        )

    resolved_status, resolved_conclusion = resolve_check_state(status, conclusion)
    payload: dict[str, Any] = {
        "name": name,
        "head_sha": head_sha,
        "status": resolved_status.value,
        "output": {"title": title, "summary": summary},
    }
    if resolved_conclusion is not None:
        payload["conclusion"] = resolved_conclusion.value
    if details_url:
        payload["details_url"] = details_url
    if external_id:
        payload["external_id"] = external_id
    return payload


def job_dashboard_url(dashboard_url: str, job_id: str) -> str:
    """Build the absolute URL of one job's report on the Scout dashboard.

    Why a helper: the check run is the only place a reviewer is handed this link, and a
    typo or a doubled slash produces a 404 in the one artefact they trust. Callers pass
    the dashboard *root* (``SCOUT_DASHBOARD_URL``); the job path is appended here.

    Args:
        dashboard_url: Dashboard root, e.g. ``http://localhost:5173``.
        job_id: Scout job id, e.g. ``job_01J8ZQ4K2M7X3N9PQRSTVWXY0``.

    Raises:
        GitHubError: When the dashboard URL or job id is empty, because a check run
            without a working evidence link is worse than one that fails loudly.
    """
    base = dashboard_url.strip().rstrip("/")
    if not base:
        raise GitHubError(
            "the Scout dashboard URL is not configured; set SCOUT_DASHBOARD_URL so the "
            "check run can link to the evidence"
        )
    if not job_id.strip():
        raise GitHubError("cannot build a dashboard link without a job id")
    return f"{base}{DASHBOARD_JOB_PATH_TEMPLATE.format(job_id=job_id.strip())}"


def truncate_check_summary(summary: str, *, dashboard_url: str) -> str:
    """Clamp a Markdown summary to GitHub's 65 535-character limit.

    Deterministic on purpose: the same input always produces byte-identical output, so
    two runs of the same job do not produce two different check-run bodies, and a
    truncation is reproducible when someone asks why a paragraph is cut off.

    Args:
        summary: Full Markdown summary.
        dashboard_url: Absolute dashboard URL used by the truncation notice. The notice
            exists so the reader is never left with a silently partial report.

    Returns:
        ``summary`` unchanged when it fits, otherwise a prefix plus a notice linking to
        the dashboard, totalling at most :data:`MAX_CHECK_SUMMARY_CHARS` characters.
    """
    if len(summary) <= MAX_CHECK_SUMMARY_CHARS:
        return summary

    notice = _TRUNCATION_NOTICE_TEMPLATE.format(
        limit=MAX_CHECK_SUMMARY_CHARS,
        dashboard_url=dashboard_url,
    )
    budget = MAX_CHECK_SUMMARY_CHARS - len(notice)
    if budget <= 0:  # pragma: no cover - defensive; the notice is far shorter
        return notice[:MAX_CHECK_SUMMARY_CHARS]
    return summary[:budget].rstrip() + notice


def render_check_summary(report: VerificationReport, dashboard_url: str) -> str:
    """Render the Markdown summary GitHub shows on the pull-request check.

    The shape is deliberate: the recommendation first (that is the answer), then the
    per-area lines from :meth:`VerificationReport.check_run_summary_lines`, then a link
    to the full report. The PR is where the engineer already is; the check must not make
    them hunt for the verdict, and it must not try to reproduce the report either.

    Args:
        report: The gated verification report. Passing the report rather than loose
            values is what makes it impossible to render a check that disagrees with the
            recommendation the report itself validated.
        dashboard_url: Dashboard root; the job page is appended.

    Returns:
        Markdown, never longer than :data:`MAX_CHECK_SUMMARY_CHARS` characters.
    """
    link = job_dashboard_url(dashboard_url, report.job_id)

    lines: list[str] = [
        f"## {CHECK_RUN_NAME}",
        "",
        f"**Recommendation:** {report.recommendation.value}  ",
        f"**Risk level:** {report.risk_level.value}  ",
        f"**Verified commit:** `{report.commit_sha}`  ",
        f"**Pull request:** {report.repository_full_name} #{report.pull_request_number}",
    ]

    if report.executive_summary.strip():
        lines.extend(
            [
                "",
                "### Summary",
                "",
                _bounded(report.executive_summary, MAX_EXECUTIVE_SUMMARY_CHARS),
            ]
        )

    lines.extend(["", "### Results", ""])
    lines.extend(f"- {line}" for line in report.check_run_summary_lines())

    if report.unverified_areas:
        lines.extend(["", "### Not verified", ""])
        lines.extend(f"- {area}" for area in report.unverified_areas)

    lines.extend(["", f"[View the full report on the Scout dashboard]({link})", ""])

    return truncate_check_summary("\n".join(lines), dashboard_url=link)


def _bounded(text: str, limit: int) -> str:
    """Return ``text`` limited to ``limit`` characters with an explicit ellipsis.

    Uses an ellipsis rather than silent truncation so a reader can tell that the excerpt
    continues on the dashboard.
    """
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rstrip() + " …"
