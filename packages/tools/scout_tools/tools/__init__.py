"""The deterministic tools Scout ships (spec section 65).

This subpackage is the concrete half of the tool framework: :mod:`scout_tools.base`
defines what a tool *is*, and every module here answers one engineering question with a
real program and a real parser.

:func:`build_default_tools` is the single entry point used by
:func:`scout_tools.registry.default_registry`. It returns instances, already filtered:

* ``only`` restricts the set by name, for a stage that wants one capability's tools.
* ``disabled`` keeps a tool in the registry but marks it disabled, so
  :meth:`ToolRegistry.plan` can explain the skip ("tool is disabled by configuration")
  instead of silently omitting it. An unexplained skip is not acceptable in a report;
  the registry's design makes the explanation the default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_tools.tools.coverage import CoverageTool
from scout_tools.tools.dependencies import NpmAuditTool, TrivyTool
from scout_tools.tools.generic import GenericCommandTool, exit_code_parser
from scout_tools.tools.linters import EslintTool, MypyTool, RuffFormatTool, RuffTool, TscTool
from scout_tools.tools.pytest import PytestTool
from scout_tools.tools.security import BanditTool, GitleaksTool, PipAuditTool, SemgrepTool

if TYPE_CHECKING:
    from scout_tools.base import Tool

__all__ = [
    "BanditTool",
    "CoverageTool",
    "EslintTool",
    "GenericCommandTool",
    "GitleaksTool",
    "MypyTool",
    "NpmAuditTool",
    "PipAuditTool",
    "PytestTool",
    "RuffFormatTool",
    "RuffTool",
    "SemgrepTool",
    "TrivyTool",
    "TscTool",
    "build_default_tools",
    "exit_code_parser",
]

#: Every tool class Scout ships, in the order they are presented in the registry.
_DEFAULT_TOOL_TYPES: tuple[type[Tool], ...] = (
    PytestTool,
    CoverageTool,
    RuffTool,
    RuffFormatTool,
    MypyTool,
    EslintTool,
    TscTool,
    GitleaksTool,
    BanditTool,
    SemgrepTool,
    PipAuditTool,
    NpmAuditTool,
    TrivyTool,
)


def build_default_tools(
    *,
    disabled: set[str] | None = None,
    only: set[str] | None = None,
) -> list[Tool]:
    """Build the registry contents.

    Args:
        disabled: Tool names to register but mark disabled. They remain visible to
            :meth:`ToolRegistry.plan` so the report can state why they did not run.
        only: When given, the returned set is restricted to these names. A name that
            matches nothing is ignored here; the registry reports an unregistered name
            that repository configuration requested.

    Returns:
        Fresh tool instances, so a caller cannot mutate another registry's tools.
    """
    tools: list[Tool] = [tool_type() for tool_type in _DEFAULT_TOOL_TYPES]
    if only is not None:
        tools = [tool for tool in tools if tool.name in only]
    if disabled:
        for tool in tools:
            if tool.name in disabled:
                tool.enabled = False
    return tools
