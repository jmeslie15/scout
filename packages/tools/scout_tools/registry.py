"""The tool registry (spec section 20).

The registry is the *only* place a tool name becomes a tool object. Agents ask for a
capability or a name; they cannot construct a command. That indirection is what makes
"agents must never directly construct arbitrary shell commands" a structural property
rather than a promise.

It also carries the applicability decision. Spec section 19 says only applicable tools
should run, and spec section 65's acceptance criteria require that a skipped tool is
explained. :meth:`ToolRegistry.plan` returns both the tools to run and the reasons others
were skipped, so the report can state the latter.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from scout_domain.enums import Capability
from scout_domain.errors import ScoutDomainError
from scout_observability import get_logger
from scout_tools.base import Tool, ToolCategory, ToolContext

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["ToolNotFoundError", "ToolPlan", "ToolRegistry", "default_registry"]

_log = get_logger(__name__)


class ToolNotFoundError(ScoutDomainError):
    """Raised when a requested tool is not registered."""


@dataclass(frozen=True, slots=True)
class ToolPlan:
    """Which tools will run, and why the rest will not."""

    selected: tuple[Tool, ...]
    skipped: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self.selected)

    def __len__(self) -> int:
        return len(self.selected)

    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.selected)

    def explain(self) -> str:
        """Render the plan for the report and for operator debugging."""
        lines: list[str] = []
        if self.selected:
            lines.append("Tools scheduled: " + ", ".join(self.names()))
        else:
            lines.append("No deterministic tools are applicable to this change.")
        if self.skipped:
            lines.append("Tools not run:")
            lines.extend(f"  - {name}: {reason}" for name, reason in self.skipped)
        return "\n".join(lines)


class ToolRegistry:
    """A registry of deterministic tools, keyed by name."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    # -- Registration ----------------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """Add a tool. Re-registering a name replaces it, which is what tests want."""
        if not getattr(tool, "name", None):
            raise ValueError(f"tool {type(tool).__name__} must declare a name")
        if tool.name in self._tools:
            _log.warning("tool replaced in registry", tool=tool.name)
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(sorted(self._tools.values(), key=lambda tool: tool.name))

    # -- Lookup ----------------------------------------------------------------------

    def get(self, name: str) -> Tool:
        """Return a tool by name, or raise :class:`ToolNotFoundError`."""
        try:
            return self._tools[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._tools)) or "<none registered>"
            raise ToolNotFoundError(
                f"unknown tool {name!r}; registered tools: {available}", tool=name
            ) from exc

    def all(self) -> tuple[Tool, ...]:
        return tuple(self)

    def by_category(self, *categories: ToolCategory) -> tuple[Tool, ...]:
        wanted = set(categories)
        return tuple(tool for tool in self if tool.category in wanted)

    def by_capability(self, capability: Capability) -> tuple[Tool, ...]:
        return tuple(tool for tool in self if tool.capability is capability)

    # -- Planning --------------------------------------------------------------------

    def plan(
        self,
        context: ToolContext,
        *,
        categories: Sequence[ToolCategory] | None = None,
        names: Sequence[str] | None = None,
    ) -> ToolPlan:
        """Decide which tools to run for ``context``.

        Args:
            context: The run's context, including the workspace and stack summary used for
                applicability.
            categories: Restrict to these categories (used per stage: the testing stage
                runs TEST tools, the security stage runs SECURITY tools).
            names: Restrict to these tool names, when the repository config declares an
                explicit tool list.

        Returns:
            A :class:`ToolPlan` listing both the selection and the explained skips.
        """
        wanted_names = set(names) if names else None
        wanted_categories = set(categories) if categories else None

        selected: list[Tool] = []
        skipped: list[tuple[str, str]] = []

        for tool in self:
            if wanted_names is not None and tool.name not in wanted_names:
                continue
            if wanted_categories is not None and tool.category not in wanted_categories:
                continue

            reason = tool.applicability_reason(context)
            if reason is None:
                selected.append(tool)
            else:
                skipped.append((tool.name, reason))

        # A name the repository config asked for that is not registered is a configuration
        # error the operator should see, not a silent no-op.
        if wanted_names is not None:
            unknown = sorted(wanted_names - set(self._tools))
            for name in unknown:
                skipped.append((name, "requested by repository configuration but not registered"))

        plan = ToolPlan(selected=tuple(selected), skipped=tuple(sorted(skipped)))
        _log.debug(
            "tool plan computed",
            job_id=context.job_id,
            selected=plan.names(),
            skipped=[name for name, _ in plan.skipped],
        )
        return plan


def default_registry() -> ToolRegistry:
    """Build the registry containing every tool Scout ships.

    Imported lazily so that a caller that only wants the interface does not pay for eleven
    tool modules, and so that a syntax error in one tool cannot break the registry for the
    others.
    """
    from scout_tools.tools import build_default_tools

    return ToolRegistry(build_default_tools())
