"""Shared harness for the deterministic tool tests.

Nothing here shells out. A tool test builds a :class:`CommandOutcome` from fixture files
and calls the tool's parser directly, or hands the outcome to a
:class:`FakeSandbox` and calls ``tool.run`` to exercise the framework's full path
(version probe, artifact capture, result construction).

The fake sandbox matters for a second reason: it lets the contract tests assert that no
tool needs a real binary. Scout's tool tests must pass on a machine with none of pytest,
ruff, mypy, bandit, gitleaks, semgrep, trivy, or npm installed.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from pathlib import Path
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

#: ``tests/fixtures/tools`` — real captured output samples, checked in as files.
FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "tools"

JOB_ID = "job_01TESTTOOLHARNESS"
COMMIT_SHA = "a" * 40

#: A stack that satisfies every ecosystem declared by the default tool set.
DEFAULT_STACK = TechnologyStack(
    languages=["python", "typescript"],
    frameworks=[],
    package_managers=["npm", "uv"],
    test_frameworks=["pytest"],
    build_tools=[],
    ci_systems=["github-actions"],
)


def load_fixture(name: str) -> str:
    """Return the text of a captured tool-output fixture."""
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8")


def write_fixture(workspace: Workspace, fixture: str, target: str) -> str:
    """Copy a fixture into the workspace at ``target`` and return ``target``."""
    workspace.write_text(target, load_fixture(fixture))
    return target


@pytest.fixture(name="load_fixture")
def _load_fixture_fixture():
    """Expose :func:`load_fixture` as a fixture (pytest 9 does not guarantee sys.path)."""
    return load_fixture


@pytest.fixture(name="write_fixture")
def _write_fixture_fixture():
    """Expose :func:`write_fixture` as a fixture."""
    return write_fixture


class FakeSandbox(Sandbox):
    """A sandbox that runs nothing and returns prepared outcomes.

    ``outcomes`` is consumed in order by :meth:`run`. Version probes (labelled
    ``<tool>-version``) never consume it: they get a canned version banner, because a
    test that queues one outcome for the tool's real command should not have it eaten by
    the framework's version probe.
    """

    backend = SandboxBackendKind.LOCAL

    def __init__(
        self,
        *,
        workspace: Workspace,
        policy: SandboxPolicy | None = None,
        limits: ResourceLimits | None = None,
        executables: Sequence[str] = (),
        outcomes: Sequence[CommandOutcome] = (),
        version_banner: str = "tool 1.2.3",
    ) -> None:
        super().__init__(
            job_id=JOB_ID,
            workspace=workspace,
            limits=limits or ResourceLimits(),
            policy=policy or SandboxPolicy(),
            run_id="sbr_01TESTTOOLHARNESS",
        )
        self.commands: list[SandboxCommand] = []
        self._executables = {name: f"/usr/local/bin/{name}" for name in executables}
        self._outcomes = list(outcomes)
        self._version_banner = version_banner

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def run(self, command: SandboxCommand) -> CommandOutcome:
        self.commands.append(command)
        if command.label.endswith("-version"):
            return CommandOutcome(
                argv=tuple(command.argv),
                exit_code=0,
                stdout_bytes=len(self._version_banner),
                duration_seconds=0.01,
            )
        if self._outcomes:
            return self._outcomes.pop(0)
        return CommandOutcome(argv=tuple(command.argv), exit_code=0, duration_seconds=0.01)

    async def resolve_executable(self, executable: str) -> str | None:
        return self._executables.get(executable)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    """A fresh, real sandbox workspace (path resolution is part of what tools use)."""
    created = Workspace.create(tmp_path / "workspaces", job_id=JOB_ID)
    try:
        yield created
    finally:
        created.close()


@pytest.fixture
def artifacts() -> ArtifactStore:
    """An in-memory artifact store, so capture is observable without touching disk."""
    return InMemoryArtifactStore()


@pytest.fixture
def make_outcome(tmp_path: Path):
    """Build a :class:`CommandOutcome` backed by real stdout/stderr files."""
    counter = itertools.count()

    def _make(
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = 0,
        argv: Sequence[str] = ("tool",),
        timed_out: bool = False,
        launch_error: str | None = None,
        duration_seconds: float = 0.25,
        signal: int | None = None,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
    ) -> CommandOutcome:
        index = next(counter)
        stdout_path = tmp_path / f"stdout-{index}.log"
        stderr_path = tmp_path / f"stderr-{index}.log"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        return CommandOutcome(
            argv=tuple(argv),
            exit_code=exit_code,
            signal=signal,
            timed_out=timed_out,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_bytes=len(stdout.encode("utf-8")),
            stderr_bytes=len(stderr.encode("utf-8")),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            duration_seconds=duration_seconds,
            launch_error=launch_error,
        )

    return _make


@pytest.fixture
def make_context(workspace: Workspace, artifacts: ArtifactStore, make_outcome: Any):
    """Build a :class:`ToolContext` around the workspace and a fake sandbox."""

    def _make(
        *,
        files: dict[str, str] | None = None,
        changed_paths: Sequence[str] = (),
        command_overrides: dict[str, Sequence[str]] | None = None,
        stack: TechnologyStack | None = None,
        with_summary: bool = True,
        policy: SandboxPolicy | None = None,
        policy_mode: NetworkMode | None = None,
        allowed_hosts: Sequence[str] = (),
        executables: Sequence[str] = (),
        outcomes: Sequence[CommandOutcome] = (),
        security_tools: Sequence[str] = (),
    ) -> ToolContext:
        for path, content in (files or {}).items():
            workspace.write_text(path, content)

        resolved_policy = policy
        if resolved_policy is None:
            mode = policy_mode or NetworkMode.NONE
            allowed = list(allowed_hosts)
            if mode is NetworkMode.ALLOWLIST and not allowed:
                allowed = ["semgrep.dev"]
            resolved_policy = SandboxPolicy(network_mode=mode, allowed_hosts=allowed)

        summary: RepositorySummary | None = None
        if with_summary:
            summary = RepositorySummary(
                repository=RepositoryRef(full_name="acme/widgets"),
                commit_sha=COMMIT_SHA,
                stack=stack or DEFAULT_STACK,
            )

        config = parse_repository_config(
            {
                "project": {"name": "widgets"},
                "security": {"tools": list(security_tools)},
            }
        )
        return ToolContext(
            job_id=JOB_ID,
            stage="testing",
            repository_full_name="acme/widgets",
            commit_sha=COMMIT_SHA,
            workspace=workspace,
            sandbox=FakeSandbox(
                workspace=workspace,
                policy=resolved_policy,
                executables=executables,
                outcomes=outcomes,
            ),
            artifacts=artifacts,
            repository_config=config,
            changed_paths=tuple(changed_paths),
            repository_summary=summary,
            command_overrides={
                key: tuple(value) for key, value in (command_overrides or {}).items()
            },
        )

    return _make
