"""The sandbox interface (spec sections 20, 35, and 53).

Everything that executes repository code goes through a :class:`Sandbox`. Tools never
touch :mod:`subprocess` directly, which is what makes the guarantees in this module
auditable: there is exactly one place in Scout where untrusted code is launched, so
there is exactly one place to review.
"""

from __future__ import annotations

import abc
from pathlib import Path

from scout_domain.enums import SandboxBackendKind
from scout_domain.models.sandbox import ResourceLimits, SandboxPolicy
from scout_sandbox.command import CommandOutcome, SandboxCommand
from scout_sandbox.workspace import Workspace

__all__ = ["Sandbox"]


class Sandbox(abc.ABC):
    """An isolated execution environment for one job.

    Implementations must uphold these invariants:

    * The command is executed from an ``argv`` list. No shell interprets repository
      input, tool arguments, or configuration values, ever.
    * Only :attr:`workspace` is writable; the rest of the host is not reachable.
    * The environment is a minimal allowlist. Control-plane variables are refused.
    * Network access follows :attr:`policy`; the default is none.
    * A timeout always terminates the process and its descendants.
    * Output is streamed to files, never buffered unbounded in memory.

    Implementations must be honest about what they do *not* guarantee. A Docker-based
    sandbox is not a hard multi-tenant boundary; see the backend docstrings.
    """

    backend: SandboxBackendKind

    def __init__(
        self,
        *,
        job_id: str,
        workspace: Workspace,
        limits: ResourceLimits,
        policy: SandboxPolicy,
        run_id: str,
    ) -> None:
        self.job_id = job_id
        self.workspace = workspace
        self.limits = limits
        self.policy = policy
        self.run_id = run_id
        self._closed = False
        #: Where stdout/stderr captures are written. Kept outside the repository tree so
        #: repository code cannot read or tamper with Scout's record of its own output.
        self.capture_dir = workspace.path.parent / f"{job_id}.capture"

    # -- Lifecycle -------------------------------------------------------------------

    @abc.abstractmethod
    async def start(self) -> None:
        """Provision the sandbox. Idempotent."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Tear the sandbox down and remove any runtime resources."""

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def __aenter__(self) -> Sandbox:
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.stop()

    # -- Execution -------------------------------------------------------------------

    @abc.abstractmethod
    async def run(self, command: SandboxCommand) -> CommandOutcome:
        """Execute ``command`` inside the sandbox and return its outcome.

        Must never raise for an ordinary non-zero exit code: that is a *result*.
        Raises :class:`~scout_sandbox.errors.SandboxError` only for infrastructure
        failures.
        """

    async def run_many(self, commands: list[SandboxCommand]) -> list[CommandOutcome]:
        """Execute commands in order, stopping after the first failure.

        Sequential by design: a test run and the coverage report that reads its artifacts
        must not race, and the initial deployment target has one CPU sandbox anyway
        (ADR 0001).
        """
        outcomes: list[CommandOutcome] = []
        for command in commands:
            outcome = await self.run(command)
            outcomes.append(outcome)
            if not outcome.succeeded:
                break
        return outcomes

    # -- Convenience -----------------------------------------------------------------

    async def run_simple(
        self,
        *argv: str,
        cwd: str = ".",
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        label: str = "",
    ) -> CommandOutcome:
        """Run a command built from discrete arguments.

        Provided so that callers never have to construct a :class:`SandboxCommand`
        inline, which is where someone would eventually be tempted to accept a string.
        """
        return await self.run(
            SandboxCommand(
                argv=list(argv),
                cwd=cwd,
                env=env or {},
                timeout_seconds=timeout_seconds or self.limits.command_timeout_seconds,
                label=label or (argv[0] if argv else ""),
            )
        )

    async def which(self, executable: str) -> str | None:
        """Return the resolved path of ``executable`` inside the sandbox, if present.

        Used by the tool layer to decide whether a tool is applicable before claiming it
        was skipped versus simply unavailable (spec section 19: only use tools applicable
        to the detected repository).

        Implemented by each backend rather than by shelling out. The name is validated
        first so that a backend which must consult a shell cannot be handed an argument
        that forms a command.
        """
        return await self.resolve_executable(validate_executable_name(executable))

    @abc.abstractmethod
    async def resolve_executable(self, executable: str) -> str | None:
        """Backend-native executable lookup. ``executable`` is already validated."""

    def capture_path(self, name: str) -> Path:
        """Return a path in the capture directory for a named stream."""
        safe = "".join(
            character if character.isalnum() or character in "-_." else "_" for character in name
        )
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        return self.capture_dir / safe


def validate_executable_name(value: str) -> str:
    """Constrain a lookup argument to a bare executable name.

    Scout executes commands as an ``argv`` list with no shell. The *only* exception is a
    backend that must ask a shell whether a binary exists (there is no portable
    non-shell equivalent inside a minimal container image). Restricting the argument to
    characters that cannot form a command keeps that exception from becoming an
    injection point.
    """
    if not value or not all(character.isalnum() or character in "-_." for character in value):
        raise ValueError(f"refusing to probe a non-trivial executable name: {value!r}")
    return value
