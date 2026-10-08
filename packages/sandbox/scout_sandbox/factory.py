"""Sandbox construction from configuration.

A single factory keeps the backend choice in one place, and — more importantly — keeps
the *policy* choice in one place. The safety properties of a sandbox come from the
combination of backend, resource limits, and network policy, so they must be decided
together rather than at each call site.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from scout_domain.config import ScoutRepositoryConfig
from scout_domain.enums import SandboxBackendKind
from scout_domain.errors import ConfigurationError
from scout_domain.models.sandbox import (
    DEFAULT_RESOURCE_LIMITS,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
)
from scout_observability import get_logger
from scout_sandbox.base import Sandbox
from scout_sandbox.docker import DockerSandbox
from scout_sandbox.local import LocalSandbox

if TYPE_CHECKING:
    from scout_sandbox.workspace import Workspace

__all__ = [
    "SANDBOX_IMAGE_ENV",
    "build_policy",
    "build_resource_limits",
    "create_sandbox",
    "resolve_backend",
    "workspace_root",
]

_log = get_logger(__name__)

#: Environment variable naming the container image for the Docker backend.
SANDBOX_IMAGE_ENV = "SCOUT_SANDBOX_IMAGE"
DEFAULT_SANDBOX_IMAGE = "scout-sandbox-python:3.12"


def resolve_backend(explicit: SandboxBackendKind | str | None = None) -> SandboxBackendKind:
    """Resolve the sandbox backend, refusing the unsafe one in production.

    The local backend runs repository code on the host. That is acceptable in
    development and unacceptable in production, so the refusal lives here rather than in
    documentation nobody reads.
    """
    raw = explicit or os.environ.get("SCOUT_SANDBOX_BACKEND", "docker")
    try:
        backend = SandboxBackendKind(str(raw).lower())
    except ValueError as exc:
        raise ConfigurationError(
            f"unknown sandbox backend {raw!r}; expected one of "
            f"{', '.join(kind.value for kind in SandboxBackendKind)}",
            variable="SCOUT_SANDBOX_BACKEND",
        ) from exc

    environment = os.environ.get("SCOUT_ENV", "development").lower()
    if backend is SandboxBackendKind.LOCAL and environment in {"production", "prod"}:
        raise ConfigurationError(
            "the local sandbox backend executes repository code on the host and is "
            "refused when SCOUT_ENV=production; use the docker backend",
            variable="SCOUT_SANDBOX_BACKEND",
            environment=environment,
        )
    if backend is SandboxBackendKind.LOCAL:
        _log.warning(
            "using the local sandbox backend: repository code will execute on this host",
            environment=environment,
        )
    return backend


def build_resource_limits(
    overrides: dict[str, object] | None = None,
    *,
    repository_config: ScoutRepositoryConfig | None = None,
) -> ResourceLimits:
    """Build resource limits from the environment, then clamp repository requests.

    A repository may ask for *less* than the platform default. It may never ask for more:
    the operator's ceiling is a hard limit, and a pull request is untrusted input
    (spec section 52).
    """
    base = ResourceLimits(
        memory_mb=int(os.environ.get("SCOUT_SANDBOX_MEMORY_MB", DEFAULT_RESOURCE_LIMITS.memory_mb)),
        cpus=float(os.environ.get("SCOUT_SANDBOX_CPUS", DEFAULT_RESOURCE_LIMITS.cpus)),
        pids_limit=int(
            os.environ.get("SCOUT_SANDBOX_PIDS_LIMIT", DEFAULT_RESOURCE_LIMITS.pids_limit)
        ),
        command_timeout_seconds=int(
            os.environ.get(
                "SCOUT_SANDBOX_COMMAND_TIMEOUT_SECONDS",
                DEFAULT_RESOURCE_LIMITS.command_timeout_seconds,
            )
        ),
    )
    if overrides:
        base = base.model_copy(update=overrides)

    requested = repository_config.sandbox if repository_config else None
    if requested is None:
        return base

    updates: dict[str, object] = {}
    if requested.memory_mb is not None:
        updates["memory_mb"] = min(requested.memory_mb, base.memory_mb)
    if requested.cpus is not None:
        updates["cpus"] = min(requested.cpus, base.cpus)
    if requested.command_timeout_seconds is not None:
        updates["command_timeout_seconds"] = min(
            requested.command_timeout_seconds, base.command_timeout_seconds
        )
    if updates:
        applied = {key: value for key, value in updates.items() if getattr(base, key) != value}
        if applied:
            _log.info(
                "repository-requested sandbox limits reduce the platform defaults",
                limits=applied,
            )
        base = base.model_copy(update=updates)
    return base


def build_policy(
    *,
    network_mode: NetworkMode | str | None = None,
    allowed_hosts: list[str] | None = None,
    repository_config: ScoutRepositoryConfig | None = None,
) -> SandboxPolicy:
    """Build the sandbox policy, defaulting to deny-all networking."""
    raw_mode = network_mode or os.environ.get("SCOUT_SANDBOX_NETWORK", NetworkMode.NONE.value)
    try:
        resolved_mode = NetworkMode(str(raw_mode).lower())
    except ValueError as exc:
        raise ConfigurationError(
            f"unknown network mode {raw_mode!r}; expected one of "
            f"{', '.join(mode.value for mode in NetworkMode)}",
            variable="SCOUT_SANDBOX_NETWORK",
        ) from exc

    # A repository can never widen the network policy; a `network_mode: open` request in
    # `.scout/engineering.yaml` is ignored rather than honoured.
    if repository_config and repository_config.sandbox.network_mode is not None:
        requested = repository_config.sandbox.network_mode
        if requested is not resolved_mode:
            _log.warning(
                "ignoring repository-requested network mode; the operator policy wins",
                requested=requested.value,
                enforced=resolved_mode.value,
            )

    hosts = list(allowed_hosts or [])
    if resolved_mode is NetworkMode.NONE and hosts:
        hosts = []

    return SandboxPolicy(
        network_mode=resolved_mode,
        allowed_hosts=hosts,
    )


def workspace_root() -> Path:
    """Return the configured root under which job workspaces are created."""
    raw = os.environ.get("SCOUT_WORKSPACE_ROOT", "./var/workspaces")
    return Path(raw).expanduser().resolve()


def create_sandbox(
    *,
    job_id: str,
    workspace: Workspace,
    run_id: str,
    backend: SandboxBackendKind | str | None = None,
    limits: ResourceLimits | None = None,
    policy: SandboxPolicy | None = None,
    image: str | None = None,
    repository_config: ScoutRepositoryConfig | None = None,
) -> Sandbox:
    """Construct the configured sandbox for one job.

    ``repository_config`` is accepted only for *limits*, which a repository may reduce. Its
    ``sandbox.backend`` and ``sandbox.network_mode`` are deliberately ignored: how strongly
    code is isolated and whether it can reach the network are operator decisions. Allowing a
    pull request to select the non-isolating local backend would let an untrusted author
    weaken the boundary around their own code.
    """
    if repository_config is not None and repository_config.sandbox.backend is not None:
        _log.warning(
            "ignoring the sandbox backend requested by repository configuration; "
            "the operator's backend is enforced",
            requested=repository_config.sandbox.backend.value,
            enforced=str(backend or os.environ.get("SCOUT_SANDBOX_BACKEND", "docker")),
        )

    resolved_backend = resolve_backend(backend)
    resolved_limits = limits or build_resource_limits()
    resolved_policy = policy or build_policy()
    resolved_image = image or os.environ.get(SANDBOX_IMAGE_ENV, DEFAULT_SANDBOX_IMAGE)

    if resolved_backend is SandboxBackendKind.DOCKER:
        return DockerSandbox(
            job_id=job_id,
            workspace=workspace,
            limits=resolved_limits,
            policy=resolved_policy,
            run_id=run_id,
            image=resolved_image,
        )
    return LocalSandbox(
        job_id=job_id,
        workspace=workspace,
        limits=resolved_limits,
        policy=resolved_policy,
        run_id=run_id,
    )
