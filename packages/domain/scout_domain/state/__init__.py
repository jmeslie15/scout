"""Job lifecycle domain logic: the state machine and the retry policy."""

from __future__ import annotations

from scout_domain.state.machine import (
    VERIFICATION_PIPELINE,
    Transition,
    TransitionContext,
    allowed_transitions,
    assert_transition,
    can_transition,
    is_forward_progress,
    next_pipeline_stage,
    pipeline_index,
    transition,
)
from scout_domain.state.retry import (
    DEFAULT_RETRY_POLICY,
    RetryPolicy,
    classify_exception,
    register_failure_class,
)

__all__ = [
    "DEFAULT_RETRY_POLICY",
    "VERIFICATION_PIPELINE",
    "RetryPolicy",
    "Transition",
    "TransitionContext",
    "allowed_transitions",
    "assert_transition",
    "can_transition",
    "classify_exception",
    "is_forward_progress",
    "next_pipeline_stage",
    "pipeline_index",
    "register_failure_class",
    "transition",
]
