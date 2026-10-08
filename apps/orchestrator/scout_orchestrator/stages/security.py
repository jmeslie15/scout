"""SECURITY_SCANNING: secrets, static security analysis, and dependency risk.

Whether this stage runs at all is the orchestrator's decision, not this one: spec section 41
lets a LOW-risk change skip it, and the repository's ``.scout/engineering.yaml`` may disable
it, subject to the operator floor. When the stage *does* run, its job is the same as REVIEWING
with a different tool set -- and, because scanners are the tools most likely to be missing
from a developer's machine, the same "missing is not the same as broke" distinction applies.

A scanner that could not run produces no finding about the pull request, only an
``UNVERIFIED`` section. That is the rule that keeps "Scout could not check for secrets" from
being reported as "Scout found no secrets" (spec sections 2 and 21).
"""

from __future__ import annotations

from typing import Final

from scout_domain.enums import JobStatus
from scout_orchestrator.stages.checkout import ensure_checkout
from scout_orchestrator.stages.context import BaseStage, StageContext, StageResult
from scout_orchestrator.stages.tool_stage import run_tool_stage
from scout_tools.base import ToolCategory

__all__ = ["SecurityStage"]

_STAGE: Final = JobStatus.SECURITY_SCANNING

#: The categories the security stage owns, most critical first. Ordering is presentation only;
#: every applicable tool in the set runs.
_SECURITY_CATEGORIES: Final[tuple[ToolCategory, ...]] = (
    ToolCategory.SECRET_SCAN,
    ToolCategory.STATIC_SECURITY,
    ToolCategory.DEPENDENCY,
    ToolCategory.CONTAINER,
)


class SecurityStage(BaseStage):
    """Run secret detection, static security analysis, and dependency scanning."""

    stage = _STAGE

    async def run(self, ctx: StageContext) -> StageResult:
        """Materialise the pinned revision and run the security tools.

        The checkout happens here because the workspace is per-stage, and a secret scanner over an
        empty directory would report a clean repository.
        """
        await ensure_checkout(ctx)
        config = ctx.require_repository_config()
        declared_tools = list(config.security.tools) or None

        bundle = await run_tool_stage(
            ctx,
            categories=_SECURITY_CATEGORIES,
            section_title="Security",
            tool_names=declared_tools,
        )
        return StageResult.succeeded_for(
            self.stage,
            narrative=_narrative(
                bundle.executed_tools, len(bundle.findings), len(bundle.unverified_tools)
            ),
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


def _narrative(executed: tuple[str, ...], finding_count: int, unverified_count: int) -> str:
    """Summarise what the security scan measured, stating what it could not."""
    if not executed:
        return "no security tool is applicable to this repository"
    base = f"ran {', '.join(executed)} and reported {finding_count} finding(s)"
    if unverified_count:
        return f"{base}; {unverified_count} check(s) could not produce a result"
    return base
