"""OpenTelemetry wiring (spec section 49).

Tracing is how a slow verification job is diagnosed: one trace spans the webhook, the
queue hop, the orchestrator decision, the worker, the sandbox run, each tool, each model
call, and the GitHub check publication. Metrics answer the aggregate questions the spec
lists — job duration, agent duration, token spend, escalation rate.

Telemetry is explicitly optional. When ``SCOUT_OTEL_ENABLED`` is false (the default in
development and in tests) the no-op API is used, so instrumentation calls cost nothing
and never introduce a hard dependency on a collector being reachable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

__all__ = [
    "METRIC_NAMES",
    "get_meter",
    "get_tracer",
    "instrument_span",
    "record_metric",
    "setup_tracing",
    "shutdown_tracing",
]

_enabled: bool = False


def setup_tracing(
    *,
    service_name: str | None = None,
    endpoint: str | None = None,
    enabled: bool | None = None,
) -> bool:
    """Configure tracing and metrics exporters.

    Returns:
        ``True`` when telemetry was enabled, ``False`` when it was left as a no-op.

    Deliberately fail-soft: if the OpenTelemetry SDK is unavailable or misconfigured, Scout
    must still run. Observability is important, but not important enough to take down
    verification (Rule 1: do not introduce infrastructure before it is necessary).
    """
    global _enabled  # noqa: PLW0603 - process-wide singleton by design

    if enabled is None:
        enabled = os.environ.get("SCOUT_OTEL_ENABLED", "false").lower() in {"1", "true", "yes"}
    if not enabled:
        _enabled = False
        return False

    resolved_service = service_name or os.environ.get("SCOUT_OTEL_SERVICE_NAME", "scout")
    resolved_endpoint = endpoint or os.environ.get(
        "SCOUT_OTEL_EXPORTER_ENDPOINT", "http://localhost:4318"
    )

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": resolved_service,
                "service.namespace": "scout",
                "deployment.environment": os.environ.get("SCOUT_ENV", "development"),
            }
        )

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{resolved_endpoint.rstrip('/')}/v1/traces")
            )
        )
        trace.set_tracer_provider(tracer_provider)

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{resolved_endpoint.rstrip('/')}/v1/metrics"),
            export_interval_millis=30_000,
        )
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

        _enabled = True
        return True
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "OpenTelemetry setup failed; continuing without tracing", exc_info=True
        )
        _enabled = False
        return False


def shutdown_tracing() -> None:
    """Flush and shut down providers. Called on graceful process exit."""
    global _enabled  # noqa: PLW0603
    if not _enabled:
        return
    try:
        from opentelemetry import metrics, trace

        tracer_provider = trace.get_tracer_provider()
        if hasattr(tracer_provider, "shutdown"):
            tracer_provider.shutdown()
        meter_provider = metrics.get_meter_provider()
        if hasattr(meter_provider, "shutdown"):
            meter_provider.shutdown()
    except Exception:
        pass
    finally:
        _enabled = False


def get_tracer(name: str) -> Any:
    """Return a tracer. Returns a no-op tracer when telemetry is disabled."""
    from opentelemetry import trace

    return trace.get_tracer(name)


def get_meter(name: str) -> Any:
    """Return a meter. Returns a no-op meter when telemetry is disabled."""
    from opentelemetry import metrics

    return metrics.get_meter(name)


@contextmanager
def instrument_span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    tracer_name: str = "scout",
) -> Iterator[Any]:
    """Open a span, recording the exception type on failure before re-raising.

    Rule 12 applies to tracing as much as to control flow: an exception is recorded and
    then propagated, never swallowed here.
    """
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is not None:
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_attribute("error.type", type(exc).__name__)
            raise


#: Metric names from spec section 49, kept in one place so dashboards and code agree.
METRIC_NAMES: Final[dict[str, str]] = {
    "jobs_total": "scout.jobs.total",
    "jobs_failed": "scout.jobs.failed",
    "job_duration": "scout.job.duration",
    "agent_duration": "scout.agent.duration",
    "tool_duration": "scout.tool.duration",
    "llm_tokens": "scout.llm.tokens",
    "llm_cost": "scout.llm.cost_usd",
    "human_escalations": "scout.human.escalations",
    "findings_by_severity": "scout.findings",
    "test_failures": "scout.tests.failures",
    "sandbox_duration": "scout.sandbox.duration",
    "github_api_calls": "scout.github.api_calls",
    "notification_deliveries": "scout.notifications.deliveries",
}


def record_metric(
    name: str,
    value: float,
    *,
    kind: str = "counter",
    attributes: Mapping[str, Any] | None = None,
    unit: str = "",
    description: str = "",
) -> None:
    """Record one metric sample.

    Uses the logical metric names in :data:`METRIC_NAMES`; an unknown name is passed
    through so a new metric does not require editing this module first.
    """
    try:
        meter = get_meter("scout")
        resolved = METRIC_NAMES.get(name, name)
        attrs = dict(attributes or {})
        if kind == "counter":
            meter.create_counter(resolved, unit=unit, description=description).add(value, attrs)
        elif kind == "histogram":
            meter.create_histogram(resolved, unit=unit, description=description).record(
                value, attrs
            )
        elif kind == "updowncounter":
            meter.create_up_down_counter(resolved, unit=unit, description=description).add(
                value, attrs
            )
        else:
            raise ValueError(f"unsupported metric kind: {kind!r}")
    except ValueError:
        raise
    except Exception:
        pass
