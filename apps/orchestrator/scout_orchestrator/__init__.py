"""Scout's control plane: job state transitions, stage scheduling, retries, and recovery.

The public surface is deliberately small. :class:`~scout_orchestrator.orchestrator.Orchestrator` is
the component a deployment runs; :mod:`scout_orchestrator.stages` holds the pipeline that both
execution planes share; :mod:`scout_orchestrator.reporting` holds the recommendation derivation,
which is pure and unit-testable on its own.
"""

from __future__ import annotations

from scout_orchestrator.errors import (
    OrchestratorError,
    ReportDerivationError,
    ResumeInvariantViolation,
    StageExecutionError,
    StageInfrastructureError,
)
from scout_orchestrator.orchestrator import Orchestrator, build_tool_registry
from scout_orchestrator.reporting import (
    RecommendationInputs,
    build_report,
    derive_recommendation,
)
from scout_orchestrator.settings import Settings, get_settings
from scout_orchestrator.state import apply_transition, skip_decision

__version__ = "0.1.0"

__all__ = [
    "Orchestrator",
    "OrchestratorError",
    "RecommendationInputs",
    "ReportDerivationError",
    "ResumeInvariantViolation",
    "Settings",
    "StageExecutionError",
    "StageInfrastructureError",
    "__version__",
    "apply_transition",
    "build_report",
    "build_tool_registry",
    "derive_recommendation",
    "get_settings",
    "skip_decision",
]
