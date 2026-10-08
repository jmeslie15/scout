"""Defensive logger resolution.

``scout-observability`` is owned by a different workstream and may be absent (or may
not yet export ``get_logger``) while this package is being built and tested. Scout must
never fail to import — let alone fail to run — because an optional observability
dependency is missing, so the lookup is defensive and falls back to the standard
library. The fallback produces the same records at the same levels; the only thing lost
is whatever structured enrichment the observability package adds.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Protocol, cast

__all__ = ["ScoutLogger", "get_logger"]


class ScoutLogger(Protocol):
    """The slice of the logging API Scout uses.

    Declared as a protocol so this module does not have to know whether
    ``scout_observability`` returns a stdlib :class:`logging.Logger` or a structlog-style
    bound logger. Calls must use ``%``-style parameters (never f-strings) so that a log
    line that is filtered out costs nothing to build, and so secrets embedded in an
    argument never get formatted into the message by accident.
    """

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None: ...

    def info(self, message: str, *args: Any, **kwargs: Any) -> None: ...

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None: ...

    def error(self, message: str, *args: Any, **kwargs: Any) -> None: ...

    def exception(self, message: str, *args: Any, **kwargs: Any) -> None: ...


def get_logger(name: str) -> ScoutLogger:
    """Return the best available logger for ``name``.

    Prefers ``scout_observability.get_logger`` when that package is installed and
    exports the factory, and otherwise returns a stdlib logger. Resolution happens per
    call rather than at import time so that a partially built observability package can
    be completed without rebuilding this one.

    Args:
        name: Module name, conventionally ``__name__``.
    """
    try:
        module = importlib.import_module("scout_observability")
    except ImportError:
        return logging.getLogger(name)

    factory = getattr(module, "get_logger", None)
    if callable(factory):
        logger = factory(name)
        if logger is not None:
            return cast("ScoutLogger", logger)
    return logging.getLogger(name)
