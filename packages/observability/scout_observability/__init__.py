"""Observability primitives shared by every Scout process.

Three concerns, one package:

* :mod:`scout_observability.context` — correlation identity propagated with ``contextvars``.
* :mod:`scout_observability.logging` — structured, redacting logs.
* :mod:`scout_observability.tracing` — optional OpenTelemetry traces and metrics.

The redaction pipeline is the part that matters most. Spec sections 38 and 55 forbid
secrets in logs and in model request records, and a rule that depends on every developer
remembering it is not a rule. Here it is a processor that runs on every event.
"""

from __future__ import annotations

from scout_observability.context import (
    bind_context,
    clear_context,
    current_context,
    get_correlation_id,
    get_job_id,
    job_context,
)
from scout_observability.logging import (
    REDACTED,
    configure_logging,
    get_logger,
    redact_mapping,
)
from scout_observability.tracing import (
    METRIC_NAMES,
    get_meter,
    get_tracer,
    instrument_span,
    record_metric,
    setup_tracing,
    shutdown_tracing,
)

__all__ = [
    "METRIC_NAMES",
    "REDACTED",
    "bind_context",
    "clear_context",
    "configure_logging",
    "current_context",
    "get_correlation_id",
    "get_job_id",
    "get_logger",
    "get_meter",
    "get_tracer",
    "instrument_span",
    "job_context",
    "record_metric",
    "redact_mapping",
    "setup_tracing",
    "shutdown_tracing",
]
