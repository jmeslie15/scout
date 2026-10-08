"""Local (host process) sandbox backend.

**This backend is not a security boundary.** It runs repository code as the current user
on the host, guarded only by POSIX resource limits and a scrubbed environment. It exists
because it makes the platform runnable, testable, and debuggable during development, and
because on macOS the Docker path cannot enforce every limit the Linux path can.

Use it only when the code under review is already trusted, or in development. The
production default is :class:`~scout_sandbox.docker.DockerSandbox`. An operator enables
this backend explicitly with ``SCOUT_SANDBOX_BACKEND=local``, and the factory refuses to
enable it when ``SCOUT_ENV=production``.
"""

from __future__ import annotations

import asyncio
import os
import resource
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from scout_domain.enums import SandboxBackendKind
from scout_domain.models.sandbox import ResourceLimits, SandboxPolicy
from scout_observability import get_logger, instrument_span
from scout_sandbox.base import Sandbox
from scout_sandbox.command import (
    CommandOutcome,
    SandboxCommand,
    build_minimal_environment,
)
from scout_sandbox.errors import SandboxCommandError

if TYPE_CHECKING:
    from scout_sandbox.workspace import Workspace

__all__ = ["LocalSandbox"]

_log = get_logger(__name__)


class LocalSandbox(Sandbox):
    """Runs commands as child processes on the host with resource limits applied."""

    backend = SandboxBackendKind.LOCAL

    def __init__(
        self,
        *,
        job_id: str,
        workspace: Workspace,
        limits: ResourceLimits,
        policy: SandboxPolicy,
        run_id: str,
    ) -> None:
        super().__init__(
            job_id=job_id, workspace=workspace, limits=limits, policy=policy, run_id=run_id
        )
        self._active_processes: set[asyncio.subprocess.Process] = set()

    # -- Lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        self.capture_dir.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        """Terminate any process still running, then mark the sandbox closed.

        A job must not leave orphaned processes behind: they consume the sandbox budget
        and, on a single-machine deployment, can starve the next job.
        """
        for process in list(self._active_processes):
            if process.returncode is None:
                _terminate_process_group(process)
        self._active_processes.clear()
        self._closed = True

    # -- Execution -------------------------------------------------------------------

    async def run(self, command: SandboxCommand) -> CommandOutcome:
        if self._closed:
            raise SandboxCommandError("sandbox has been closed", job_id=self.job_id)

        # The policy check happens here, immediately before launch, so it cannot be
        # skipped by a caller that forgot to validate its environment.
        environment = build_minimal_environment(
            extra=dict(command.env),
            forbidden=frozenset(self.policy.forbidden_environment),
        )
        self.policy.assert_environment_is_safe(environment)

        cwd = self.workspace.resolve(command.cwd)
        if not cwd.is_dir():
            cwd = self.workspace.path

        stdout_path = self._stream_path(command, "stdout")
        stderr_path = self._stream_path(command, "stderr")
        timeout = min(command.timeout_seconds, self.limits.command_timeout_seconds)

        started_at = datetime.now(UTC)
        monotonic_start = time.monotonic()

        with instrument_span(
            "sandbox.local.run",
            attributes={
                "sandbox.run_id": self.run_id,
                "job.id": self.job_id,
                "command.label": command.label,
                "command.argv0": command.executable,
            },
        ) as span:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                try:
                    process = await asyncio.create_subprocess_exec(
                        *command.argv,
                        cwd=str(cwd),
                        env=environment,
                        stdin=subprocess.DEVNULL if command.stdin is None else subprocess.PIPE,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        start_new_session=True,  # own process group, so we can kill the tree
                        preexec_fn=self._limit_setter(),
                    )
                except (FileNotFoundError, PermissionError, OSError) as exc:
                    span.set_attribute("error.type", type(exc).__name__)
                    return CommandOutcome(
                        argv=tuple(command.argv),
                        exit_code=None,
                        timed_out=False,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        duration_seconds=time.monotonic() - monotonic_start,
                        launch_error=f"{type(exc).__name__}: {exc}",
                        sandbox_run_id=self.run_id,
                    )

                self._active_processes.add(process)
                timed_out = False
                try:
                    if command.stdin is not None and process.stdin is not None:
                        process.stdin.write(command.stdin)
                        await process.stdin.drain()
                        process.stdin.close()
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except TimeoutError:
                    timed_out = True
                    _terminate_process_group(process)
                    await _await_exit(process, grace_seconds=5.0)
                finally:
                    self._active_processes.discard(process)

            duration = time.monotonic() - monotonic_start
            stdout_bytes, stdout_truncated = _enforce_capture_limit(
                stdout_path, command.capture_limit_bytes
            )
            stderr_bytes, stderr_truncated = _enforce_capture_limit(
                stderr_path, command.capture_limit_bytes
            )

        exit_code, signal_number = _decode_returncode(process.returncode, timed_out)
        span.set_attribute("command.exit_code", exit_code if exit_code is not None else -1)
        span.set_attribute("command.duration_seconds", duration)

        if timed_out:
            _log.warning(
                "sandbox command timed out",
                job_id=self.job_id,
                label=command.label,
                timeout_seconds=timeout,
            )

        return CommandOutcome(
            argv=tuple(command.argv),
            exit_code=exit_code,
            signal=signal_number,
            timed_out=timed_out,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            duration_seconds=duration,
            sandbox_run_id=self.run_id,
        )

    async def resolve_executable(self, executable: str) -> str | None:
        return shutil.which(executable, path=os.environ.get("PATH", ""))

    # -- Internals -------------------------------------------------------------------

    def _stream_path(self, command: SandboxCommand, stream: str) -> Path:
        """Return a capture path unique per command, ordered, and filesystem-safe."""
        label = command.label or command.executable
        safe = "".join(
            character if character.isalnum() or character in "-_." else "_" for character in label
        )
        # A monotonically increasing counter keeps ordering readable when one tool runs
        # the same label several times (retries, parametrised suites).
        existing = len(list(self.capture_dir.glob(f"*_{safe}_{stream}.log")))
        return self.capture_path(f"{existing:03d}_{safe}_{stream}.log")

    def _limit_setter(self) -> Callable[[], None]:
        """Build a ``preexec_fn`` that applies POSIX resource limits in the child.

        Notes on fidelity, because overstating these limits would be worse than not
        having them:

        * ``RLIMIT_CPU`` and ``RLIMIT_FSIZE`` are reliable on macOS and Linux.
        * ``RLIMIT_AS`` (address space) is enforced on Linux but is unreliable on macOS,
          which is why the Docker backend is the real memory control.
        * ``RLIMIT_NPROC`` is per-user on macOS, not per-process-tree, so it is applied
          only on Linux.
        """
        limits = self.limits
        cpu_seconds = int(limits.lifetime_seconds)
        file_size_bytes = limits.disk_mb * 1024 * 1024
        is_linux = os.uname().sysname == "Linux"

        def _apply() -> None:  # pragma: no cover - runs in the forked child
            try:
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_size_bytes, file_size_bytes))
                if is_linux:
                    memory_bytes = limits.memory_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
                    resource.setrlimit(
                        resource.RLIMIT_NPROC, (limits.pids_limit, limits.pids_limit)
                    )
                resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            except (ValueError, OSError):
                # A limit the platform refuses is recorded by the caller's tests rather
                # than crashing the child before exec.
                pass

        return _apply


def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Send SIGTERM to the process group, escalating to SIGKILL.

    Signalling the *group* matters because a test runner spawns workers; killing only the
    parent leaves the children running and the job reports success while work continues.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except ProcessLookupError:
            return


async def _await_exit(process: asyncio.subprocess.Process, *, grace_seconds: float) -> None:
    """Wait briefly for a terminated process, then SIGKILL the group."""
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        pass


def _decode_returncode(returncode: int | None, timed_out: bool) -> tuple[int | None, int | None]:
    """Split a negative ``returncode`` into ``(exit_code, signal)``."""
    if returncode is None:
        return None, signal.SIGKILL if timed_out else None
    if returncode < 0:
        return None, -returncode
    return returncode, None


def _enforce_capture_limit(path: Path, limit_bytes: int) -> tuple[int, bool]:
    """Truncate a capture file to ``limit_bytes``, returning ``(size, truncated)``."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0, False
    if size <= limit_bytes:
        return size, False
    with path.open("r+b") as handle:
        handle.truncate(limit_bytes)
        handle.seek(0, os.SEEK_END)
        handle.write(b"\n<... output truncated by Scout capture limit ...>\n")
    return limit_bytes, True
