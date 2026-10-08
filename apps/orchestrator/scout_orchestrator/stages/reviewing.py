"""REVIEWING: static analysis over the change.

The stage is thin on purpose. Which linters and type checkers to run, how to invoke each one,
and how to read its output are all owned by the tool layer (spec sections 19-21); this stage
contributes only the decision of *which categories* belong to a review and the boundary that
turns a broken tool into an unverified section rather than a finding about the pull request.

Applicability is decided by the tools themselves from the repository's own declarations -- a
pre-commit config, a ``[tool.ruff]`` block, a ``tsconfig.json``. Spec section 19 requires that
only applicable tools run, and a tool that does not apply is recorded with its reason so the
report can say "ruff was not run because this repository does not configure it".
"""

from __future__ import annotations

from typing import Final

from scout_domain.enums import JobStatus
from scout_orchestrator.stages.checkout import ensure_checkout
from scout_orchestrator.stages.context import BaseStage, StageContext, StageResult
from scout_orchestrator.stages.tool_stage import run_tool_stage
from scout_tools.base import ToolCategory

__all__ = ["ReviewingStage"]

_STAGE: Final = JobStatus.REVIEWING

#: The categories a code review owns. Security scanners and test runners are other stages'
#: business, which keeps the report's sections aligned with the question each one answers.
_REVIEW_CATEGORIES: Final[tuple[ToolCategory, ...]] = (
    ToolCategory.LINT,
    ToolCategory.FORMAT,
    ToolCategory.TYPECHECK,
    ToolCategory.BUILD,
)


class ReviewingStage(BaseStage):
    """Run the repository's declared linters, formatters, and type checkers."""

    stage = _STAGE

    async def run(self, ctx: StageContext) -> StageResult:
        """Materialise the pinned revision and run the repository's declared review tools.

        The checkout happens here because the workspace is per-stage: a linter has nothing to read
        in a directory that was created seconds ago and never populated.
        """
        await ensure_checkout(ctx)
        bundle = await run_tool_stage(
            ctx,
            categories=_REVIEW_CATEGORIES,
            section_title="Code review",
        )
        return StageResult.succeeded_for(
            self.stage,
            narrative=_narrative(bundle.executed_tools, len(bundle.findings)),
            tool_results=bundle.results,
            findings=bundle.findings,
            artifacts=ctx.artifacts.take(),
            sections=bundle.sections,
            payload={
                "executed_tools": list(bundle.executed_tools),
                "skipped_tools": [list(item) for item in bundle.skipped_tools],
                "unverified_tools": [list(item) for item in bundle.unverified_tools],
                "limitations": list(bundle.limitations()),
            },
        )


def _narrative(executed: tuple[str, ...], finding_count: int) -> str:
    """Summarise what the review measured."""
    if not executed:
        return "no code-review tool is applicable to this repository"
    return f"ran {', '.join(executed)} and reported {finding_count} finding(s)"
