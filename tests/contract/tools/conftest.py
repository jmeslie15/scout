"""Harness for the tool *contract* tests.

These tests check properties of the tool set as a whole — declarations, argument
vectors, registry planning, and the ``ToolResult`` contract — rather than the parsing
behaviour of any one tool. The unit suite has a richer harness
(``tests/unit/tools/conftest.py``); this one is deliberately small and self-contained so
the contract suite can run on its own.

Nothing here shells out: every tool is exercised against a :class:`FakeSandbox` that
returns prepared outcomes.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from typing import Any

import pytest

from scout_domain.config.repository import parse_repository_config
from scout_domain.enums import SandboxBackendKind
from scout_domain.models.repository import RepositoryRef, RepositorySummary, TechnologyStack
from scout_domain.models.sandbox import NetworkMode, ResourceLimits, SandboxPolicy
from scout_sandbox import Sandbox, SandboxCommand, Workspace
from scout_sandbox.command import CommandOutcome
from scout_tools.artifacts import ArtifactStore, InMemoryArtifactStore
from scout_tools.base import ToolContext

JOB_ID = "job_01TESTCONTRACT"
COMMIT_SHA = "b" * 40

#: A stack that satisfies every ecosystem the default tool set declares.
DEFAULT_STACK = TechnologyStack(
    languages=["python", "typescript"],
    package_managers=["npm", "uv"],
    test_frameworks=["pytest"],
    ci_systems=["github-actions"],
)


class FakeSandbox(Sandbox):
    """A sandbox that records commands and returns prepared outcomes."""

    backend = SandboxBackendKind.LOCAL

    def __init__(
        self,
        *,
        workspace: Workspace,
        policy: SandboxPolicy | None = None,
        outcomes: Sequence[CommandOutcome] = (),
    ) -> None:
        super().__init__(
            job_id=JOB_ID,
            workspace=workspace,
            limits=ResourceLimits(),
            policy=policy or SandboxPolicy(),
            run_id="sbr_01TESTCONTRACT",
        )
        self.commands: list[SandboxCommand] = []
        self._outcomes = list(outcomes)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run(self, command: SandboxCommand) -> CommandOutcome:
        self.commands.append(command)
        if command.label.endswith("-version"):
            return CommandOutcome(argv=tuple(command.argv), exit_code=0, duration_seconds=0.01)
        if self._outcomes:
            return self._outcomes.pop(0)
        return CommandOutcome(argv=tuple(command.argv), exit_code=0, duration_seconds=0.01)

    async def resolve_executable(self, executable: str) -> str | None:
        return f"/usr/local/bin/{executable}"


@pytest.fixture
def workspace(tmp_path) -> Iterator[Workspace]:
    created = Workspace.create(tmp_path / "workspaces", job_id=JOB_ID)
    try:
        yield created
    finally:
        created.close()


@pytest.fixture
def artifacts() -> ArtifactStore:
    return InMemoryArtifactStore()


@pytest.fixture
def make_outcome(tmp_path):
    counter = itertools.count()

    def _make(
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = 0,
        argv: Sequence[str] = ("tool",),
        timed_out: bool = False,
        launch_error: str | None = None,
        duration_seconds: float = 0.1,
    ) -> CommandOutcome:
        index = next(counter)
        stdout_path = tmp_path / f"stdout-{index}.log"
        stderr_path = tmp_path / f"stderr-{index}.log"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        return CommandOutcome(
            argv=tuple(argv),
            exit_code=exit_code,
            timed_out=timed_out,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_bytes=len(stdout.encode("utf-8")),
            stderr_bytes=len(stderr.encode("utf-8")),
            duration_seconds=duration_seconds,
            launch_error=launch_error,
        )

    return _make


@pytest.fixture
def make_context(workspace: Workspace, artifacts: ArtifactStore, make_outcome: Any):
    def _make(
        *,
        files: dict[str, str] | None = None,
        changed_paths: Sequence[str] = (),
        with_summary: bool = True,
        policy_mode: NetworkMode = NetworkMode.NONE,
        allowed_hosts: Sequence[str] = (),
        security_tools: Sequence[str] = (),
        outcomes: Sequence[CommandOutcome] = (),
    ) -> ToolContext:
        for path, content in (files or {}).items():
            workspace.write_text(path, content)

        allowed = list(allowed_hosts)
        if policy_mode is NetworkMode.ALLOWLIST and not allowed:
            allowed = ["semgrep.dev"]

        summary: RepositorySummary | None = None
        if with_summary:
            summary = RepositorySummary(
                repository=RepositoryRef(full_name="acme/widgets"),
                commit_sha=COMMIT_SHA,
                stack=DEFAULT_STACK,
            )

        return ToolContext(
            job_id=JOB_ID,
            stage="testing",
            repository_full_name="acme/widgets",
            commit_sha=COMMIT_SHA,
            workspace=workspace,
            sandbox=FakeSandbox(
                workspace=workspace,
                policy=SandboxPolicy(network_mode=policy_mode, allowed_hosts=allowed),
                outcomes=outcomes,
            ),
            artifacts=artifacts,
            repository_config=parse_repository_config(
                {"project": {"name": "widgets"}, "security": {"tools": list(security_tools)}}
            ),
            changed_paths=tuple(changed_paths),
            repository_summary=summary,
        )

    return _make
