"""Publishing Scout's conclusion to GitHub as a check run (spec section 45).

The check run is Scout's *only* write to GitHub, and it is the single place a reviewer is handed
the verdict, so two things are non-negotiable:

* the conclusion comes from :func:`scout_github.conclusion_for_recommendation` applied to the
  gated report -- never from a caller with an opinion. ``REQUIRES_HUMAN_REVIEW`` therefore maps
  to ``neutral`` by construction, and a red check can never mean "Scout has not decided".
* the summary is rendered from the report object itself, so the check cannot disagree with the
  report it links to.

The returned check-run id is recorded on the audit ledger. ``verification_jobs`` has no column
for it (adding one is a schema change outside this component's ownership), and the ledger is
append-only and indexed by job, which is what an operator actually queries when asking "which
check run did Scout create for this job?".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from scout_domain.models.artifact import ArtifactKind
from scout_github import (
    CHECK_RUN_NAME,
    CheckRunConclusion,
    conclusion_for_recommendation,
    job_dashboard_url,
    render_check_summary,
)
from scout_observability import get_logger, instrument_span

if TYPE_CHECKING:
    from scout_domain.models.artifact import Artifact
    from scout_domain.models.job import VerificationJob
    from scout_domain.models.report import VerificationReport
    from scout_github import GitHubClient
    from scout_tools.artifacts import ArtifactStore

__all__ = [
    "CHECK_SUMMARY_FILENAME",
    "CheckRunPublication",
    "publish_check_run",
    "summary_artifact",
]

_log = get_logger(__name__)

#: Filename used for the artifact holding the exact check-run summary Scout published. Kept as
#: evidence so a reviewer can see what GitHub was told even if the check run is later edited.
CHECK_SUMMARY_FILENAME: Final = "github-check-summary.md"


@dataclass(frozen=True, slots=True)
class CheckRunPublication:
    """What was published: the check run id, its conclusion, and the summary as evidence."""

    check_run_id: int
    conclusion: CheckRunConclusion
    summary: str
    details_url: str


async def publish_check_run(
    *,
    github: GitHubClient,
    job: VerificationJob,
    report: VerificationReport,
    dashboard_url: str,
) -> CheckRunPublication:
    """Create the check run for ``report`` and return what was published.

    Raises:
        GitHubError: (typed, from ``scout_github``) when GitHub refuses the request. The caller
            records the failure and classifies it for retry; it must not be swallowed, because a
            verification nobody sees is a verification that did not happen.
    """
    summary = render_check_summary(report, dashboard_url)
    details_url = job_dashboard_url(dashboard_url, job.id)
    conclusion = conclusion_for_recommendation(report.recommendation)

    with instrument_span(
        "orchestrator.check_run.publish",
        attributes={
            "job.id": job.id,
            "github.repository": job.repository_full_name,
            "github.head_sha": job.head_sha,
            "report.recommendation": report.recommendation.value,
        },
    ):
        check_run = await github.create_check_run(
            job.repository_full_name,
            job.head_sha,
            CHECK_RUN_NAME,
            conclusion=conclusion,
            title=f"Scout recommends {report.recommendation.value}",
            summary=summary,
            details_url=details_url,
            external_id=job.id,
        )

    _log.info(
        "published GitHub check run",
        job_id=job.id,
        check_run_id=check_run.id,
        conclusion=conclusion.value,
    )
    return CheckRunPublication(
        check_run_id=int(check_run.id),
        conclusion=conclusion,
        summary=summary,
        details_url=details_url,
    )


def summary_artifact(
    publication: CheckRunPublication,
    *,
    store: ArtifactStore,
    job_id: str,
    sandbox_run_id: str | None = None,
) -> Artifact:
    """Persist the published summary as evidence and return its locator record."""
    return store.put_text(
        publication.summary,
        kind=ArtifactKind.LOG,
        content_type="text/markdown",
        filename=CHECK_SUMMARY_FILENAME,
        job_id=job_id,
        sandbox_run_id=sandbox_run_id,
    )
