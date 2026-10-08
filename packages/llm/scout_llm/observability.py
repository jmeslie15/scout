"""Defensive access to ``scout_observability``.

``packages/observability`` is owned by a different workstream and may be absent, empty, or
still changing while ``scout_llm`` is built. Importing it at module scope with a bare
``from scout_observability import get_logger`` would make this package unimportable in that
window, so the lookup is done through :mod:`importlib` and falls back to a stdlib logger.

The fallback is not a silent degradation: the log level and message format are identical
for the purpose this package uses it for (a diagnostic line), and ``scout_llm`` emits no
metrics or traces of its own — cost and token accounting travel through
``ModelRequestRecord``, which is a domain contract rather than an observability one.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import cast

__all__ = ["get_logger"]

_LOGGER_FACTORY: Callable[[str], logging.Logger] | None = None


def _resolve_logger_factory() -> Callable[[str], logging.Logger]:
    """Return ``scout_observability.get_logger`` when it exists, else ``logging.getLogger``."""
    try:
        module = importlib.import_module("scout_observability")
    except ImportError:
        return logging.getLogger

    candidate = getattr(module, "get_logger", None)
    if callable(candidate):
        return cast("Callable[[str], logging.Logger]", candidate)
    return logging.getLogger


def get_logger(name: str) -> logging.Logger:
    """Return a logger for ``name``, preferring Scout's observability configuration."""
    global _LOGGER_FACTORY  # noqa: PLW0603 - one-time process-wide resolution
    if _LOGGER_FACTORY is None:
        _LOGGER_FACTORY = _resolve_logger_factory()
    return _LOGGER_FACTORY(name)
