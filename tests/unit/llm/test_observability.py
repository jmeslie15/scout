"""The defensive access to ``scout_observability``.

``packages/observability`` is owned by another workstream. ``scout_llm`` must log through it
when it is available and must not become unimportable when it is not, so both branches of the
resolver are exercised here.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator

import pytest

from scout_llm import observability


@pytest.fixture(autouse=True)
def _reset_logger_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Undo the module-level logger-factory cache around every test."""
    monkeypatch.setattr(observability, "_LOGGER_FACTORY", None)
    yield
    observability._LOGGER_FACTORY = None


def test_prefers_the_observability_package_when_it_is_installed() -> None:
    resolved = observability._resolve_logger_factory()

    importable = True
    try:
        importlib.import_module("scout_observability")
    except ImportError:  # pragma: no cover - depends on the other workstream's progress
        importable = False

    if importable:
        assert resolved is not logging.getLogger
    else:  # pragma: no cover
        assert resolved is logging.getLogger


def test_falls_back_to_stdlib_logging_when_observability_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An absent package must degrade to a working logger, not an ImportError."""

    def absent(name: str) -> object:
        raise ImportError(f"no module named {name!r}")

    monkeypatch.setattr(observability.importlib, "import_module", absent)

    assert observability._resolve_logger_factory() is logging.getLogger


def test_falls_back_when_observability_exposes_no_get_logger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = type("Fake", (), {})()

    def present(_name: str) -> object:
        return fake_module

    monkeypatch.setattr(observability.importlib, "import_module", present)

    assert observability._resolve_logger_factory() is logging.getLogger


def test_get_logger_returns_a_usable_logger() -> None:
    logger = observability.get_logger("scout_llm.test")

    assert callable(getattr(logger, "warning", None))
    assert callable(getattr(logger, "info", None))
    assert callable(getattr(logger, "exception", None))


def test_the_logger_factory_is_resolved_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def counting_factory(name: str) -> logging.Logger:
        calls.append(name)
        return logging.getLogger(name)

    monkeypatch.setattr(observability, "_LOGGER_FACTORY", counting_factory)

    observability.get_logger("first")
    observability.get_logger("second")

    assert calls == ["first", "second"]
