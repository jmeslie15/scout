"""Declaring a repository-specific tool instead of writing a module for it.

Not every repository is verified by the tools Scout ships. Some have a bespoke checker, a
migration linter, or an in-house policy script. Before this module, each of those meant a
new Python module, a new test, and a new release. :class:`GenericCommandTool` makes it a
declaration: a name, an argument template, a capability, and a parser.

**The template is an argv list, never a command string.** ``argv_template`` is a sequence
of argument elements, each of which may contain ``{placeholder}`` fields::

    GenericCommandTool(
        name="migration-check",
        argv_template=["alembic", "check", "--config", "{workspace}/alembic.ini"],
        capability=Capability.TEST_RUN,
        category=ToolCategory.TEST,
        marker_files=frozenset({"alembic.ini"}),
        parser=exit_code_parser(finding_exit_codes=frozenset({1})),
    )

Substitution happens *after* the argument vector is formed, and only into a single
element; the result is passed to the sandbox as ``argv``. There is no shell anywhere in
Scout's execution path, so a value containing ``;``, ``$(...)`` or a newline is an
ordinary argument, not a command. Recognised placeholders are:

``{workspace}``
    The workspace root as seen from inside the sandbox (``.``).
``{job_id}``, ``{stage}``, ``{repository}``, ``{commit_sha}``
    Run identity, for tools that write per-run output.
``{report_path}``
    The tool's declared report location (see ``report_file``), or a default under
    ``.scout/reports/``.
``{changed_paths_csv}``
    The changed paths joined with commas, for a tool that accepts one list argument.
``{changed_paths}``
    As a whole template element, expands to *many* arguments (one per changed path).

An unknown placeholder is rejected when the tool is constructed, so a typo fails at
registry-build time rather than as a mystery inside the sandbox.
"""

from __future__ import annotations

import string
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from scout_domain.enums import Capability, ToolStatus
from scout_tools.base import CommandTool, ToolCategory, ToolOutcome
from scout_tools.tools._common import (
    launch_failure,
    nonzero_without_evidence,
    report_path,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from scout_sandbox.command import CommandOutcome
    from scout_tools.base import ToolContext

__all__ = ["GenericCommandTool", "exit_code_parser"]

#: A parser turns a sandbox outcome into a Scout outcome. It must be total: it may be
#: handed an empty, truncated, or garbage capture, a launch error, or a timeout.
Parser = Callable[["CommandOutcome", "ToolContext"], ToolOutcome]

_ALLOWED_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {
        "changed_paths",
        "changed_paths_csv",
        "commit_sha",
        "job_id",
        "report_path",
        "repository",
        "stage",
        "workspace",
    }
)

#: Template elements that expand to several argv elements rather than one.
_LIST_PLACEHOLDERS: Final[frozenset[str]] = frozenset({"{changed_paths}"})

_FORMATTER: Final[string.Formatter] = string.Formatter()


class GenericCommandTool(CommandTool):
    """A tool declared as data: an argv template plus a parser."""

    def __init__(
        self,
        *,
        name: str,
        argv_template: Sequence[str],
        capability: Capability,
        category: ToolCategory,
        parser: Parser,
        description: str = "",
        marker_files: Iterable[str] = (),
        ecosystems: Iterable[str] = (),
        version_argv: Sequence[str] = (),
        report_file: str | None = None,
        finding_exit_codes: Iterable[int] = (),
        override_key: str | None = None,
        enabled: bool = True,
        timeout_seconds: int | None = None,
    ) -> None:
        super().__init__(enabled=enabled, timeout_seconds=timeout_seconds)
        if not name:
            raise ValueError("a generic tool must declare a name")
        if not argv_template:
            raise ValueError(f"generic tool {name!r} must declare a non-empty argv_template")
        if not callable(parser):
            raise TypeError(f"generic tool {name!r} must declare a callable parser")

        self.name = name
        self.capability = capability
        self.category = category
        self.description = description or f"repository-declared tool {name!r}"
        self.marker_files = frozenset(marker_files)
        self.ecosystems = frozenset(ecosystems)
        self.version_argv = tuple(version_argv)
        self.finding_exit_codes = frozenset(finding_exit_codes)
        self._parser = parser
        self._report_file = report_file or report_path(f"{name}.json")
        #: When declared, a repository-configurable command key (``test``, ``lint``,
        #: ``typecheck``, ``format``, ``build``) whose value replaces this argv entirely.
        self._override_key = override_key
        self._template = tuple(self._validate(list(argv_template)))

    # -- Declaration validation ------------------------------------------------------

    @staticmethod
    def _validate(template: list[str]) -> list[str]:
        for element in template:
            if not isinstance(element, str):
                raise TypeError(
                    "argv_template elements must be strings; Scout never accepts a "
                    f"command string, got {type(element).__name__}"
                )
            if "\x00" in element:
                raise ValueError("argv_template elements must not contain NUL bytes")
            GenericCommandTool._check_placeholders(element)
        return template

    @staticmethod
    def _check_placeholders(element: str) -> None:
        try:
            parsed = list(_FORMATTER.parse(element))
        except ValueError as exc:
            raise ValueError(
                f"argv_template element is not a valid format string: {element!r}"
            ) from exc
        for _literal, field_name, _format_spec, _conversion in parsed:
            if field_name is None:
                continue
            if field_name not in _ALLOWED_PLACEHOLDERS:
                allowed = ", ".join(sorted(_ALLOWED_PLACEHOLDERS))
                raise ValueError(
                    f"unknown placeholder {field_name!r} in argv_template element "
                    f"{element!r}; allowed placeholders are: {allowed}"
                )

    # -- Command ---------------------------------------------------------------------

    @property
    def report_file(self) -> str:
        """Workspace-relative path this tool may write its report to."""
        return self._report_file

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        if self._override_key is not None:
            declared = context.override(self._override_key)
            if declared is not None:
                # The repository knows how to run its own tool; Scout runs exactly what
                # was declared (already tokenised and metacharacter-checked by the domain
                # configuration parser) and leaves report parsing to the declared parser.
                return list(declared)
        values = self._substitutions(context)
        argv: list[str] = []
        for element in self._template:
            if element in _LIST_PLACEHOLDERS:
                # A whole element expands to one argument per changed path. Substitution
                # into a single element could never express that without a shell.
                argv.extend(context.changed_paths)
                continue
            argv.append(element.format(**values))
        return argv

    def _substitutions(self, context: ToolContext) -> dict[str, str]:
        return {
            "workspace": ".",
            "job_id": context.job_id,
            "stage": context.stage,
            "repository": context.repository_full_name,
            "commit_sha": context.commit_sha,
            "report_path": self._report_file,
            "changed_paths_csv": ",".join(context.changed_paths),
        }

    # -- Interpretation --------------------------------------------------------------

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        return self._parser(outcome, context)


def exit_code_parser(
    *,
    finding_exit_codes: Iterable[int],
    passed_narrative: str = "the tool reported no problems",
    failed_narrative: str = "the tool reported problems",
) -> Parser:
    """Build the parser for a tool whose *exit code* is its only machine-readable output.

    This is the honest fallback for a repository-specific checker: its declared exit codes
    are the contract, so a finding exit code becomes ``FAILED`` (with no fabricated
    finding), ``0`` becomes ``PASSED``, and anything else becomes ``ERROR``. A tool that
    prints structured output should get a real parser instead.
    """
    codes = frozenset(finding_exit_codes)

    def parse(outcome: CommandOutcome, _context: ToolContext) -> ToolOutcome:
        broken = launch_failure("generic tool", outcome)
        if broken is not None:
            return broken
        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        if exit_code in codes:
            return ToolOutcome(
                status=ToolStatus.FAILED,
                narrative=f"{failed_narrative} (exit code {exit_code}).",
            )
        if exit_code == 0:
            return ToolOutcome(status=ToolStatus.PASSED, narrative=passed_narrative)
        return nonzero_without_evidence("generic tool", outcome, what="interpretable output")

    return parse
