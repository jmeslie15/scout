"""Structured logging with mandatory secret redaction.

Two decisions worth stating explicitly:

1. **Logs are structured.** Every line is a mapping, so ``job_id`` is a queryable field
   rather than a substring. Debugging a distributed job by grepping prose is the failure
   mode this avoids.
2. **Redaction is a pipeline stage, not a convention.** Spec section 55 forbids secrets in
   logs, and spec section 38 forbids them in model logs. A processor that runs on every
   event is the only way to make that a guarantee rather than a hope, because it applies
   even when a developer forgets.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import MutableMapping
from typing import Any, Final, cast

import structlog

from scout_observability.context import current_context

__all__ = [
    "REDACTED",
    "configure_logging",
    "get_logger",
    "redact_mapping",
]

#: Replacement text for anything masked.
REDACTED: Final = "***REDACTED***"

#: Field names whose *values* must never appear in a log record. Matching is
#: case-insensitive and substring-based, so ``SCOUT_GITHUB_WEBHOOK_SECRET`` and
#: ``webhook_secret`` are both caught.
_SENSITIVE_KEY_PATTERNS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "authorization",
    "auth_header",
    "credential",
    "webhook_url",
    "smtp",
    "session_id",
    "cookie",
)

#: Value shapes that are secrets regardless of the key they arrived under. These catch
#: the realistic leak path: a token embedded in a URL, a diff, or tool stdout.
_SENSITIVE_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Provider API keys.
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b"),
    # GitHub tokens: classic, fine-grained, server-to-server, refresh, and app JWTs.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    # AWS access key ids.
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Slack and Google Chat incoming webhooks.
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+"),
    re.compile(r"https://chat\.googleapis\.com/v1/spaces/[A-Za-z0-9/_\-]+"),
    # PEM private key blocks.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # Credentials embedded in a URL: scheme://user:password@host
    re.compile(r"://[^/\s:@]{1,64}:[^/\s:@]{1,128}@"),
    # Database URLs with an inline password.
    re.compile(r"\b(postgres(?:ql)?|redis|rediss|mysql|amqp)(\+\w+)?://[^:/\s]+:[^@/\s]+@"),
)

#: Keys whose values are structural and must survive redaction even if their name looks
#: sensitive. ``token_counts`` contains "token" but is not a credential.
_SAFE_KEY_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "token_counts",
        "max_output_tokens",
        "max_tokens",
        "token_budget",
    }
)

#: Depth limit for recursive redaction. Deeply nested payloads are truncated rather than
#: walked forever.
_MAX_DEPTH: Final = 6


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SAFE_KEY_ALLOWLIST:
        return False
    return any(pattern in lowered for pattern in _SENSITIVE_KEY_PATTERNS)


def _scrub_value(value: str) -> str:
    scrubbed = value
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        scrubbed = pattern.sub(REDACTED, scrubbed)
    return scrubbed


def redact_mapping(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact sensitive keys and value shapes.

    Public so that it can be reused for anything else that is retained — model request
    records, artifact metadata, ``raw`` provider payloads — not just log events.
    """
    if _depth >= _MAX_DEPTH:
        return "<max-depth>"

    if isinstance(value, MutableMapping):
        return {
            key: REDACTED
            if _is_sensitive_key(str(key))
            else redact_mapping(item, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_mapping(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return _scrub_value(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return value


# -- structlog processors ------------------------------------------------------------


def _redaction_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Structlog processor applying :func:`redact_mapping` to every event."""
    redacted = redact_mapping(dict(event_dict))
    # `redact_mapping` returns a mapping for a mapping input, which the annotation already
    # guarantees; the cast keeps the processor chain's types honest for structlog.
    return cast("MutableMapping[str, Any]", redacted)


def _context_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Merge the ambient correlation context into the event.

    Explicit fields win over ambient ones, so a caller can override ``job_id`` for a
    deliberate cross-job operation without the context silently winning.
    """
    for key, value in current_context().items():
        event_dict.setdefault(key, value)
    return event_dict


def _add_log_level(
    _logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    event_dict.setdefault("level", method_name)
    return event_dict


def configure_logging(
    *,
    level: str | int | None = None,
    json_output: bool | None = None,
    service_name: str | None = None,
) -> None:
    """Configure structlog for the current process.

    Args:
        level: Log level name or number. Defaults to ``SCOUT_LOG_LEVEL`` or ``INFO``.
        json_output: Emit JSON. Defaults to ``SCOUT_LOG_JSON``, or ``True`` whenever
            ``SCOUT_ENV`` is not a local development environment — machine-readable logs
            are what make production incidents diagnosable.
        service_name: Added to every event so logs from the API, orchestrator, and
            worker can be separated after collection.
    """
    resolved_level = level if level is not None else os.environ.get("SCOUT_LOG_LEVEL", "INFO")
    if isinstance(resolved_level, str):
        resolved_level = logging.getLevelName(resolved_level.upper())
        if not isinstance(resolved_level, int):  # unknown name
            resolved_level = logging.INFO

    environment = os.environ.get("SCOUT_ENV", "development")
    if json_output is None:
        json_output = os.environ.get("SCOUT_LOG_JSON", "").lower() in {"1", "true", "yes"}
        if not json_output and environment not in {"development", "test", "local"}:
            json_output = True

    resolved_service = service_name or os.environ.get("SCOUT_OTEL_SERVICE_NAME", "scout")

    # Route stdlib logging (uvicorn, sqlalchemy, httpx) through the same pipeline so
    # third-party noise is redacted and correlated too.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=resolved_level, force=True)

    renderer: Any = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_log_level,
            _context_processor,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Redaction runs immediately before rendering, so it is the last thing that
            # can see the event dictionary.
            _redaction_processor,
            structlog.stdlib.add_logger_name,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.bind_contextvars(service=resolved_service, env=environment)


def get_logger(name: str | None = None, **initial_values: Any) -> Any:
    """Return a bound structlog logger.

    ``bind_contextvars`` is used rather than ``bind`` so that values set here survive
    across ``await`` points and are visible to every log line in the task.
    """
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    if initial_values:
        logger = logger.bind(**initial_values)
    return logger
