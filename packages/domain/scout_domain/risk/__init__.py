"""Risk-based execution depth classification."""

from __future__ import annotations

from scout_domain.risk.classifier import (
    DEFAULT_RISK_RULES,
    ApprovalGate,
    RiskAssessment,
    RiskRule,
    RiskSignal,
    classify_change_set,
    classify_paths,
    gate_for_risk,
)

__all__ = [
    "DEFAULT_RISK_RULES",
    "ApprovalGate",
    "RiskAssessment",
    "RiskRule",
    "RiskSignal",
    "classify_change_set",
    "classify_paths",
    "gate_for_risk",
]
