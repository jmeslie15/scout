"""Defensive access to ``scout_observability``.

Observability is owned by a separate workstream and may be absent or incomplete while the
platform is under construction. Notification delivery must never fail because a logging
helper is missing, so the import is dynamic and every caller receives a working logger
either way: the shared Scout logger when available, a stdlib ``logging`` logger otherwise.

The indirection is deliberate rather than a temporary hack. A notification that never
reaches a human because an import failed is the worst possible failure mode for this
package, and Rule 12 says a failure must not be hidden — but this is not hiding one, it is
choosing the fallback that keeps delivering.
"""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any

__all__ = ["get_logger"]

try:
    _OBSERVABILITY: ModuleType | None = importlib.import_module("scout_observability")
except ImportError:  # pragma: no cover - depends on the install layout, not on this code
    _OBSERVABILITY = None


def get_logger(name: str) -> Any:
    """Return the Scout logger for ``name``, falling back to stdlib ``logging``."""
    if _OBSERVABILITY is not None:
        factory = getattr(_OBSERVABILITY, "get_logger", None)
        if callable(factory):
            return factory(name)
    return logging.getLogger(name)
