"""The verification pipeline's stages, one module per stage.

``PIPELINE_STAGES`` maps a :class:`~scout_domain.enums.JobStatus` to the stage that implements
it. The mapping is a value rather than a chain of ``if`` statements so that the orchestrator's
dispatch and the registry cannot drift: a stage that is missing from the mapping is a
``KeyError`` at the moment it is needed, not a job that silently skips a step.

Both the orchestrator (in ``in_process`` execution mode) and the worker build their stage from
:func:`build_stage`, which is what keeps "the same stage code runs in either process" true.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from scout_domain.enums import JobStatus
from scout_orchestrator.stages.analyzing import AnalyzingStage
from scout_orchestrator.stages.context import (
    BaseStage,
    StageContext,
    StageInputError,
    StageInputs,
    StageResult,
    StageSettings,
)
from scout_orchestrator.stages.feature_verification import FeatureVerificationStage
from scout_orchestrator.stages.investigation import InvestigationStage
from scout_orchestrator.stages.prepared import PreparedState
from scout_orchestrator.stages.preparing import PreparingStage
from scout_orchestrator.stages.reviewing import ReviewingStage
from scout_orchestrator.stages.security import SecurityStage
from scout_orchestrator.stages.testing import TestingStage

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "PIPELINE_STAGES",
    "AnalyzingStage",
    "BaseStage",
    "FeatureVerificationStage",
    "InvestigationStage",
    "PreparedState",
    "PreparingStage",
    "ReviewingStage",
    "SecurityStage",
    "StageContext",
    "StageInputError",
    "StageInputs",
    "StageResult",
    "StageSettings",
    "TestingStage",
    "build_stage",
    "pipeline_stage_types",
]

#: One stage implementation per pipeline status. The pipeline order itself lives in
#: :data:`scout_domain.state.machine.VERIFICATION_PIPELINE`, which is the single source of
#: truth for sequencing; this mapping only answers "who implements this step".
PIPELINE_STAGES: Final[Mapping[JobStatus, type[BaseStage]]] = {
    JobStatus.PREPARING: PreparingStage,
    JobStatus.ANALYZING: AnalyzingStage,
    JobStatus.REVIEWING: ReviewingStage,
    JobStatus.SECURITY_SCANNING: SecurityStage,
    JobStatus.TESTING: TestingStage,
    JobStatus.FEATURE_VERIFYING: FeatureVerificationStage,
    JobStatus.INVESTIGATING: InvestigationStage,
}


def build_stage(stage: JobStatus) -> BaseStage:
    """Return a fresh stage instance for ``stage``.

    Raises:
        KeyError: when ``stage`` is not a pipeline stage. That is a programming error -- the
            caller asked for a status the pipeline does not implement -- and failing here is
            how it stays visible.
    """
    return PIPELINE_STAGES[stage]()


def pipeline_stage_types() -> Mapping[JobStatus, type[BaseStage]]:
    """Return the stage-type registry, for diagnostics and tests."""
    return dict(PIPELINE_STAGES)
