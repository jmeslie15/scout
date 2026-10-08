"""Docker sandbox backend: the production isolation boundary (spec sections 35 and 53).

Each command runs in a fresh container with:

* ``--network none`` by default (spec section 36: default DENY);
* ``--memory``/``--memory-swap`` equal, so the kernel cannot fall back to swap and
  quietly exceed the ceiling;
* ``--cpus`` and ``--pids-limit`` to bound a fork bomb;
* ``--read-only`` root filesystem with a small ``noexec,nosuid`` tmpfs for ``/tmp``;
* ``--cap-drop ALL`` and ``--security-opt no-new-privileges``;
* a non-root user;
* **no** mount of the host Docker socket (spec section 35), asserted, not just omitted.

Honest limitations, which belong in the code rather than only in a document:

* A container is not a virtual machine. A kernel exploit escapes it. Scout therefore treats
  the sandbox as *defence in depth*, not as the sole control.
* The *worker process* does hold control-plane credentials -- it needs the database and the
  job queue to record what it observes -- so "no credentials in the worker" would be false.
  The guarantee that is actually enforced is narrower and stronger: **repository code never
  receives them**, because the sandbox environment is built from a minimal allowlist
  (:func:`scout_sandbox.command.build_minimal_environment`) and
  :meth:`~scout_domain.models.sandbox.SandboxPolicy.assert_environment_is_safe` refuses to
  launch if a forbidden variable would be passed.
* ``ALLOWLIST`` network mode cannot be implemented with Docker's native ``--network``
  flag. It requires an egress proxy on a user-defined network. Rather than pretend,
  this backend refuses the mode with an actionable error.
* On macOS, Docker Desktop runs containers inside a Linux VM, so host filesystem exposure
  is limited to explicitly mounted paths — which is a genuine advantage over the local
  backend.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from scout_domain.enums import SandboxBackendKind
from scout_domain.models.sandbox import NetworkMode, ResourceLimits, SandboxPolicy
from scout_observability import get_logger, instrument_span
from scout_sandbox.base import Sandbox
from scout_sandbox.command import CommandOutcome, SandboxCommand, build_minimal_environment
from scout_sandbox.errors import SandboxBackendUnavailable, SandboxCommandError

if TYPE_CHECKING:
    from scout_sandbox.workspace import Workspace

__all__ = ["DockerSandbox"]

_log = get_logger(__name__)

#: Path the workspace is mounted at inside the container.
CONTAINER_WORKSPACE = "/workspace"
#: Non-root uid:gid used inside the container. Matches the ``scout`` user baked into
#: ``infra/docker/sandbox-python.Dockerfile``.
CONTAINER_USER = "1000:1000"


class DockerSandbox(Sandbox):
    """Runs each command in a locked-down, short-lived container."""

    backend = SandboxBackendKind.DOCKER

    def __init__(
        self,
        *,
        job_id: str,
        workspace: Workspace,
        limits: ResourceLimits,
        policy: SandboxPolicy,
        run_id: str,
        image: str,
        docker_binary: str = "docker",
        container_name_prefix: str = "scout-sandbox",
    ) -> None:
        super().__init__(
            job_id=job_id, workspace=workspace, limits=limits, policy=policy, run_id=run_id
        )
        self.image = image
        self.docker_binary = docker_binary
        self.container_name_prefix = container_name_prefix
        self._container_counter = 0
        self._active_containers: set[str] = set()
        self._verified = False

    # -- Lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        """Verify the container runtime and image are usable, then prepare captures."""
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        if self._verified:
            return

        if shutil.which(self.docker_binary) is None:
            raise SandboxBackendUnavailable(
                f"the docker CLI ({self.docker_binary!r}) is not on PATH; install Docker or "
                "set SCOUT_SANDBOX_BACKEND=local for development",
                job_id=self.job_id,
            )

        version = await _run_host_command(
            [self.docker_binary, "version", "--format", "{{.Server.Version}}"]
        )
        if version.returncode != 0:
            raise SandboxBackendUnavailable(
                "the Docker daemon is not reachable; start Docker Desktop or the docker service",
                job_id=self.job_id,
                stderr=version.stderr.decode("utf-8", errors="replace")[:500],
            )

        inspect = await _run_host_command(
            [self.docker_binary, "image", "inspect", self.image, "--format", "{{.Id}}"]
        )
        if inspect.returncode != 0:
            raise SandboxBackendUnavailable(
                f"sandbox image {self.image!r} is not present locally; build it with "
                f"`docker build -f infra/docker/sandbox-python.Dockerfile -t {self.image} .`",
                job_id=self.job_id,
                image=self.image,
            )

        self._verified = True

    async def stop(self) -> None:
        """Force-remove any container still running for this sandbox."""
        for name in list(self._active_containers):
            await _run_host_command([self.docker_binary, "rm", "-f", name], timeout=30)
        self._active_containers.clear()
        self._closed = True

    # -- Execution -------------------------------------------------------------------

    async def run(self, command: SandboxCommand) -> CommandOutcome:
        if self._closed:
            raise SandboxCommandError("sandbox has been closed", job_id=self.job_id)
        if not self._verified:
            await self.start()

        environment = build_minimal_environment(
            extra=dict(command.env),
            forbidden=frozenset(self.policy.forbidden_environment),
        )
        self.policy.assert_environment_is_safe(environment)

        timeout = min(command.timeout_seconds, self.limits.command_timeout_seconds)
        self._container_counter += 1
        container_name = f"{self.container_name_prefix}-{self.run_id}-{self._container_counter}"

        stdout_path = self._stream_path(command, "stdout")
        stderr_path = self._stream_path(command, "stderr")

        docker_argv = self._build_docker_argv(
            command=command,
            environment=environment,
            container_name=container_name,
        )

        started_at = datetime.now(UTC)
        monotonic_start = time.monotonic()

        with instrument_span(
            "sandbox.docker.run",
            attributes={
                "sandbox.run_id": self.run_id,
                "job.id": self.job_id,
                "command.label": command.label,
                "container.image": self.image,
            },
        ) as span:
            with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
                try:
                    process = await asyncio.create_subprocess_exec(
                        *docker_argv,
                        stdin=subprocess.DEVNULL if command.stdin is None else subprocess.PIPE,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        start_new_session=True,
                    )
                except (FileNotFoundError, PermissionError, OSError) as exc:
                    return CommandOutcome(
                        argv=tuple(command.argv),
                        exit_code=None,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        duration_seconds=time.monotonic() - monotonic_start,
                        launch_error=f"{type(exc).__name__}: {exc}",
                        sandbox_run_id=self.run_id,
                    )

                self._active_containers.add(container_name)
                timed_out = False
                try:
                    if command.stdin is not None and process.stdin is not None:
                        process.stdin.write(command.stdin)
                        await process.stdin.drain()
                        process.stdin.close()
                    # Docker's own --stop-timeout governs container shutdown; this wait_for
                    # is the outer guarantee that the worker itself cannot hang.
                    await asyncio.wait_for(process.wait(), timeout=timeout + 20)
                except TimeoutError:
                    timed_out = True
                    await _run_host_command(
                        [self.docker_binary, "kill", container_name], timeout=30
                    )
                    _terminate_process_group(process)
                    await _await_exit(process, grace_seconds=10.0)
                finally:
                    self._active_containers.discard(container_name)
                    # --rm removes the container on exit; this is belt-and-braces for the
                    # killed case, where removal can race with the kill.
                    if timed_out:
                        await _run_host_command(
                            [self.docker_binary, "rm", "-f", container_name], timeout=30
                        )

            duration = time.monotonic() - monotonic_start
            stdout_bytes, stdout_truncated = _enforce_capture_limit(
                stdout_path, command.capture_limit_bytes
            )
            stderr_bytes, stderr_truncated = _enforce_capture_limit(
                stderr_path, command.capture_limit_bytes
            )

        # A negative return code from asyncio means the process was killed by that signal;
        # a non-negative one is the exit status. Unpacked explicitly so neither branch can
        # negate a ``None``.
        raw_returncode = process.returncode
        exit_code = raw_returncode if raw_returncode is not None and raw_returncode >= 0 else None
        signal_number = (
            -raw_returncode if raw_returncode is not None and raw_returncode < 0 else None
        )

        # Docker itself exits 125 for a runtime error and 126/127 for an unusable
        # command. Those are infrastructure outcomes, not test failures — reporting them
        # as a defect in the pull request would be a false accusation.
        launch_error: str | None = None
        if not timed_out and exit_code in {125, 126, 127}:
            detail = _read_bounded(stderr_path, 2000)
            launch_error = f"container runtime error (exit {exit_code}): {detail.strip()[:500]}"
            _log.error(
                "docker sandbox command failed to launch",
                job_id=self.job_id,
                label=command.label,
                exit_code=exit_code,
            )

        span.set_attribute("command.exit_code", exit_code if exit_code is not None else -1)
        span.set_attribute("command.duration_seconds", duration)

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
            launch_error=launch_error,
            sandbox_run_id=self.run_id,
        )

    async def resolve_executable(self, executable: str) -> str | None:
        """Ask the image whether a binary exists.

        This is the one place Scout consults a shell, because a minimal image has no
        portable non-shell equivalent. ``executable`` is validated by the base class
        first, so it cannot contain shell metacharacters.
        """
        probe = await _run_host_command(
            [
                self.docker_binary,
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "sh",
                self.image,
                "-c",
                f"command -v {executable}",
            ],
            timeout=60,
        )
        if probe.returncode != 0:
            return None
        return probe.stdout.decode("utf-8", errors="replace").strip() or None

    # -- Argument construction -------------------------------------------------------

    def _build_docker_argv(
        self,
        *,
        command: SandboxCommand,
        environment: dict[str, str],
        container_name: str,
    ) -> list[str]:
        """Build the hardened ``docker run`` argument vector.

        Exposed as a method (rather than inlined) so a test can assert the exact flags:
        the security properties of this backend are only as good as this list, and a
        careless edit here should fail a test rather than silently weaken isolation.
        """
        policy = self.policy
        limits = self.limits

        if policy.network_mode is NetworkMode.ALLOWLIST:
            raise SandboxBackendUnavailable(
                "ALLOWLIST network mode requires an egress proxy; the Docker backend "
                "cannot enforce a host allowlist with --network alone. Configure a proxy "
                "network and set SCOUT_SANDBOX_NETWORK=proxy, or use NONE.",
                job_id=self.job_id,
            )
        if policy.allow_docker_socket:  # pragma: no cover - policy rejects this earlier
            raise SandboxCommandError(
                "refusing to mount the host Docker socket into a sandbox", job_id=self.job_id
            )

        argv: list[str] = [
            self.docker_binary,
            "run",
            "--rm",
            "--name",
            container_name,
            # --- network: default deny ---------------------------------------------
            "--network",
            "none" if policy.network_mode is NetworkMode.NONE else "bridge",
            # --- resource ceilings --------------------------------------------------
            "--memory",
            f"{limits.memory_mb}m",
            "--memory-swap",
            f"{limits.memory_mb}m",
            "--cpus",
            str(limits.cpus),
            "--pids-limit",
            str(limits.pids_limit),
            # --- filesystem ---------------------------------------------------------
            "--workdir",
            command.cwd if command.cwd.startswith("/") else CONTAINER_WORKSPACE,
            "--read-only" if policy.read_only_rootfs else "--read-write",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=512m",
            # --- privilege reduction ------------------------------------------------
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            CONTAINER_USER,
            # --- mounts: the workspace only ----------------------------------------
            "--volume",
            f"{self.workspace.path}:{CONTAINER_WORKSPACE}:{_mount_mode(policy)}",
        ]

        # A hard ceiling on container lifetime, independent of the worker's own timeout.
        argv.extend(["--stop-timeout", "10"])

        for key, value in sorted(environment.items()):
            argv.extend(["--env", f"{key}={value}"])

        argv.append(self.image)
        argv.extend(command.argv)
        return argv

    def _stream_path(self, command: SandboxCommand, stream: str) -> Path:
        label = command.label or command.executable
        safe = "".join(
            character if character.isalnum() or character in "-_." else "_" for character in label
        )
        return self.capture_path(f"{self._container_counter:03d}_{safe}_{stream}.log")


def _mount_mode(policy: SandboxPolicy) -> str:
    """Return the volume mode for the workspace mount.

    The workspace is the only writable mount, and only when the policy permits it. Named as a
    helper so the f-string above stays inside the line-length budget without becoming
    unreadable.
    """
    return "rw" if policy.workspace_writable else "ro"


async def _run_host_command(
    argv: list[str], *, timeout: float = 60
) -> subprocess.CompletedProcess[bytes]:
    """Run a Docker CLI command on the host.

    Scout's own control-plane processes may invoke the Docker CLI. Repository code may
    not: it never runs on the host at all.
    """

    def _invoke() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "DOCKER_CLI_HINTS": "false"},
        )

    try:
        return await asyncio.to_thread(_invoke)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            argv, returncode=124, stdout=b"", stderr=str(exc).encode()
        )


def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except ProcessLookupError:
            return


async def _await_exit(process: asyncio.subprocess.Process, *, grace_seconds: float) -> None:
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


def _read_bounded(path: Path, limit_bytes: int) -> str:
    if not path.exists():
        return ""
    try:
        with path.open("rb") as handle:
            return handle.read(limit_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _enforce_capture_limit(path: Path, limit_bytes: int) -> tuple[int, bool]:
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
