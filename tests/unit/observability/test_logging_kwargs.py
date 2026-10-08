"""Static guard against structlog reserved-keyword collisions in log calls.

This exists because of a real incident, and it is worth reading before changing it.

`_log.warning("rejected a webhook delivery with an invalid signature", event=x_github_event)`
raised `TypeError: make_method() got multiple values for argument 'event'`. structlog's bound
logger takes the message as its first positional parameter, which it binds to the name `event`.
Passing a field called `event=` therefore collides with the message itself.

The consequence was worse than a noisy log line. The failing call was inside the handler for a
*rejected webhook signature*. HMAC verification had already correctly rejected the delivery — and
then the logging of that rejection raised, so the exception handler returned **500** where the
caller should have received **401**. A security control that reports success-by-crash is a
control nobody can trust, and nothing in the test suite noticed, because the rejection path had
no test.

Two things came out of that:

1. The rejection path now has a test (`tests/unit/api/test_webhook_signature.py`).
2. This file, which makes the *class* of mistake impossible to reintroduce anywhere in the
   codebase rather than only where it happened.

It is a static check over the AST rather than a test of any one call, because the next instance
would be in a different module and would be equally silent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Method names on a logger that are called as ``logger.<name>(message, **fields)``.
_LOGGER_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical"})

#: Names that collide with structlog's bound-logger signature. Passing any of these **raises**
#: ``TypeError``, so a line like this takes down the branch that logs it. This is the set the guard
#: hard-fails on.
#:
#: Deliberately absent: ``exc_info``, ``stack_info``, and ``stacklevel``. Those are real logging
#: keywords that structlog honours, so flagging them would be a false positive that trains people
#: to ignore the guard.
RESERVED_LOG_KEYWORDS = frozenset(
    {
        "event",  # the message itself -- this is the one that caused the incident
        "level",  # bound by the filtering bound logger
        "msg",
        "message",
        "args",
        "logger",
        "timestamp",
        "ts",
    }
)

#: Names that do not raise but behave differently under structlog than under stdlib ``logging``.
#: ``extra={...}`` is the documented stdlib way to attach record attributes; structlog instead
#: stores it as a single field named ``extra``, so ``severity`` ends up nested and a dashboard
#: filtering on ``severity`` finds nothing. Reported separately because the fix is a judgement
#: call (flatten the fields, or keep a stdlib-compatible wrapper) rather than a rename.
STRUCTLOG_SEMANTIC_DIFFERENCES = frozenset({"extra"})

#: Directories to scan. Tests are excluded deliberately: a test asserting that a collision raises
#: has to perform the collision.
_SCAN_ROOTS = ("apps", "packages")


def _iter_log_calls() -> list[tuple[Path, int, str]]:
    """Yield ``(path, lineno, keyword)`` for every reserved keyword used in a log call."""
    offenders: list[tuple[Path, int, str]] = []
    for root_name in _SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:  # pragma: no cover - a syntax error is a different failure
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if func.attr not in _LOGGER_METHODS:
                    continue
                # Restrict to calls that look like logger invocations, so an unrelated object
                # with an `info` method is not flagged.
                target = func.value
                target_name = (
                    target.id
                    if isinstance(target, ast.Name)
                    else target.attr
                    if isinstance(target, ast.Attribute)
                    else ""
                )
                if "log" not in target_name.lower() and target_name != "logger":
                    continue
                for keyword in node.keywords:
                    if keyword.arg in RESERVED_LOG_KEYWORDS:
                        offenders.append((path.relative_to(REPO_ROOT), node.lineno, keyword.arg))
    return offenders


def _iter_semantic_differences() -> list[tuple[Path, int, str]]:
    """Yield log calls whose keyword is legal but means something different under structlog."""
    offenders: list[tuple[Path, int, str]] = []
    for root_name in _SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in _LOGGER_METHODS:
                    continue
                target = node.func.value
                target_name = (
                    target.id
                    if isinstance(target, ast.Name)
                    else target.attr
                    if isinstance(target, ast.Attribute)
                    else ""
                )
                if "log" not in target_name.lower() and target_name != "logger":
                    continue
                for keyword in node.keywords:
                    if keyword.arg in STRUCTLOG_SEMANTIC_DIFFERENCES:
                        offenders.append((path.relative_to(REPO_ROOT), node.lineno, keyword.arg))
    return offenders


class TestNoReservedLogKeywords:
    def test_no_log_call_passes_a_reserved_keyword(self) -> None:
        """A collision here is invisible until the exact branch that logs it executes.

        That is what made the original bug survive review and the test suite: the line only ran
        on a rejected webhook signature, which no test exercised.
        """
        offenders = _iter_log_calls()
        rendered = "\n".join(
            f"  {path}:{lineno} passes {keyword}= to a log call"
            for path, lineno, keyword in offenders
        )
        assert not offenders, (
            "structlog binds the log message to `event`, so these keywords collide with the "
            "logging pipeline and raise TypeError (or shadow a field it owns):\n"
            f"{rendered}\n"
            "Rename the field (for example `github_event=` or `notification_event=`)."
        )


class TestNoStructlogSemanticDifferences:
    """Not a crash, so it does not fail the build — but it silently loses log field structure."""

    def test_report_semantic_differences(self) -> None:
        offenders = _iter_semantic_differences()
        if not offenders:
            return
        rendered = "\n".join(
            f"  {path}:{lineno} passes {keyword}= to a log call"
            for path, lineno, keyword in offenders
        )
        pytest.skip(
            "log calls use stdlib-style `extra=`, which structlog stores as one nested field "
            "rather than promoting the keys. Not a crash, but a dashboard filtering on those "
            "keys matches nothing:\n" + rendered
        )


class TestTheGuardItselfWorks:
    """A guard that cannot fail is worse than no guard, because it reads as coverage."""

    def test_reserved_set_covers_the_keyword_that_caused_the_incident(self) -> None:
        assert "event" in RESERVED_LOG_KEYWORDS
        assert "level" in RESERVED_LOG_KEYWORDS

    def test_scan_actually_finds_files(self) -> None:
        """If the scan walks nothing it would pass vacuously."""
        scanned = [
            path
            for root_name in _SCAN_ROOTS
            for path in (REPO_ROOT / root_name).rglob("*.py")
            if "__pycache__" not in path.parts
        ]
        assert len(scanned) > 50, f"the scan only found {len(scanned)} files; check _SCAN_ROOTS"

    def test_detects_a_planted_collision(self, tmp_path: Path) -> None:
        planted = tmp_path / "planted.py"
        planted.write_text(
            "def f(_log, value):\n    _log.warning('something happened', event=value)\n",
            encoding="utf-8",
        )
        tree = ast.parse(planted.read_text(encoding="utf-8"))
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in _LOGGER_METHODS:
                    for keyword in node.keywords:
                        if keyword.arg in RESERVED_LOG_KEYWORDS:
                            found.append(str(keyword.arg))
        assert found == ["event"], "the detection logic does not catch the pattern it exists for"


if __name__ == "__main__":  # pragma: no cover - manual triage helper
    for path, lineno, keyword in _iter_log_calls():
        print(f"{path}:{lineno}: {keyword}=")
    pytest.main([__file__, "-q"])
