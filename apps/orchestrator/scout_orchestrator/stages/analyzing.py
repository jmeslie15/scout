"""ANALYZING: describe the repository and the architectural impact of the change.

The deterministic half of this stage is not a fallback -- it is the primary product. Stack,
layout, service roots, test roots, and the changed-file census are read from the checkout and
are the same on every run over the same commit (spec section 15). The model-assisted narrative
is layered on top only when the gateway is usable, and the narrative always says which of the
two produced it (see :mod:`scout_orchestrator.llm_analysis`).

That ordering matters because the architecture narrative ends up in a report a human acts on.
A model's prose about "the auth boundary" is useful context; it is not evidence, and this
stage never lets it become any.
"""

from __future__ import annotations

from typing import Final

from scout_domain.enums import EvidenceKind, JobStatus
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.report import ReportSection, ReportSectionStatus
from scout_orchestrator.llm_analysis import AnalysisNarrative, analyse_repository
from scout_orchestrator.stages.artifacts import ANALYZING_ARTIFACT_FILENAME
from scout_orchestrator.stages.context import BaseStage, StageContext, StageResult

__all__ = ["AnalyzingStage", "architecture_impact_section"]

_STAGE: Final = JobStatus.ANALYZING


class AnalyzingStage(BaseStage):
    """Produce the repository summary narrative and record architectural impact."""

    stage = _STAGE

    async def run(self, ctx: StageContext) -> StageResult:
        """Analyse the checkout and return the architecture narrative."""
        summary = ctx.require_repository_summary()
        change_set = ctx.require_change_set()

        narrative: AnalysisNarrative = await analyse_repository(
            settings=ctx.settings,
            job=ctx.job,
            summary=summary,
            change_set=change_set,
        )
        # Persisted so a report generated after a resume quotes the same narrative this stage
        # produced, whether it came from a model or from the deterministic floor.
        narrative_artifact = ctx.artifacts.put_text(
            narrative.text,
            kind=ArtifactKind.REPOSITORY_SUMMARY,
            content_type="text/markdown",
            filename=ANALYZING_ARTIFACT_FILENAME,
            job_id=ctx.job.id,
            sandbox_run_id=ctx.sandbox_run_id,
        )

        return StageResult.succeeded_for(
            self.stage,
            narrative=narrative.note,
            agent_runs=() if narrative.agent_run is None else (narrative.agent_run,),
            artifacts=ctx.artifacts.take(),
            sections=(architecture_impact_section(narrative),),
            payload={
                "architecture_impact": narrative.text,
                "architecture_impact_artifact_id": narrative_artifact.id,
                "model_assisted": narrative.used_model,
                "analysis_note": narrative.note,
                **narrative.provenance,
            },
        )


def architecture_impact_section(narrative: AnalysisNarrative) -> ReportSection:
    """Render the architecture narrative as a report section.

    The section is ``INFO`` when a model produced it and also when the deterministic floor
    did, because neither is a verification result: it is context. The ``summary`` states which
    one it was so the distinction survives into the rendered report.
    """
    return ReportSection(
        key="analyzing.architecture_impact",
        title="Architecture impact",
        status=ReportSectionStatus.INFO,
        summary=narrative.note,
        details=[narrative.text[:4000]],
        evidence=[
            EvidenceReference(
                kind=EvidenceKind.REPOSITORY_INSPECTION,
                reference_id="analyzing:architecture-impact",
                summary=(
                    "the narrative was produced from the detected stack, layout, and the "
                    "pinned change set"
                ),
            )
        ],
    )
