"""Sandbox policy and run records (spec sections 35 and 53).

Repository code is untrusted. The trust model decreases from control plane, to AI
workers, to sandbox, and this module encodes the bottom of that pyramid. Note the
defaults: network denied, no host Docker socket, no control-plane credentials, bounded
CPU/memory/PIDs. A caller must opt *out* of isolation explicitly, which is exactly the
opposite of the usual default.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from scout_domain.enums import SandboxBackendKind
from scout_domain.models.base import ScoutModel, utcnow

__all__ = [
    "DEFAULT_RESOURCE_LIMITS",
    "NetworkMode",
    "ResourceLimits",
    "SandboxPolicy",
    "SandboxRun",
    "SandboxStatus",
]


class NetworkMode(StrEnum):
    """Network posture for a sandbox (spec section 36). Default is DENY."""

    NONE = "none"
    #: Egress permitted only to an explicit allowlist enforced by the backend.
    ALLOWLIST = "allowlist"
    #: Full egress. Development only; refused when ``SCOUT_ENV=production``.
    OPEN = "open"


class SandboxStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class ResourceLimits(ScoutModel):
    """Hard resource ceilings applied to every sandbox.

    Sized for the initial deployment target -- an Apple Silicon M1 Mac Mini with 16 GB, per
    ADR 0001, which chose memory capacity over CPU generation precisely because a sandbox
    running ``npm ci`` or ``pytest`` is the bursty consumer here. The defaults are one
    sandbox at a time, 2 GiB, one CPU. These are configuration, not constants, so they can
    rise when Scout moves to a Linux worker.
    """

    memory_mb: int = Field(default=2048, ge=128, le=65_536)
    cpus: float = Field(default=1.0, gt=0.0, le=64.0)
    pids_limit: int = Field(default=256, ge=16, le=32_768)
    #: Wall-clock ceiling for a single command.
    command_timeout_seconds: int = Field(default=900, ge=1, le=86_400)
    #: Ceiling for the whole sandbox lifetime, including setup and teardown.
    lifetime_seconds: int = Field(default=3600, ge=1, le=86_400)
    #: Scratch space available inside the workspace.
    disk_mb: int = Field(default=4096, ge=64, le=1_048_576)

    @model_validator(mode="after")
    def _validate(self) -> ResourceLimits:
        if self.command_timeout_seconds > self.lifetime_seconds:
            raise ValueError("command_timeout_seconds must not exceed lifetime_seconds")
        return self


DEFAULT_RESOURCE_LIMITS = ResourceLimits()


class SandboxPolicy(ScoutModel):
    """Everything the sandbox backend must enforce for this run."""

    network_mode: NetworkMode = NetworkMode.NONE
    #: Hostnames permitted when ``network_mode`` is ALLOWLIST.
    allowed_hosts: list[str] = Field(default_factory=list)

    read_only_rootfs: bool = True
    #: The workspace is the only writable mount.
    workspace_writable: bool = True
    #: Mounting the host container runtime is how a sandbox escapes. Never allowed.
    allow_docker_socket: bool = False
    #: Privileged containers defeat the entire isolation model.
    allow_privileged: bool = False
    no_new_privileges: bool = True
    drop_all_capabilities: bool = True
    #: Linux user namespace remapping, where supported.
    run_as_non_root: bool = True

    #: Environment variable names that must never be passed into a sandbox.
    forbidden_environment: list[str] = Field(
        default_factory=lambda: [
            "SCOUT_DATABASE_URL",
            "SCOUT_REDIS_URL",
            "SCOUT_GITHUB_PRIVATE_KEY_PATH",
            "SCOUT_GITHUB_WEBHOOK_SECRET",
            "SCOUT_OPENAI_API_KEY",
            "SCOUT_ANTHROPIC_API_KEY",
            "SCOUT_SMTP_PASSWORD",
            "SCOUT_GOOGLE_CHAT_WEBHOOK_URL",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "GITHUB_TOKEN",
        ]
    )

    @model_validator(mode="after")
    def _validate_policy(self) -> SandboxPolicy:
        if self.allow_docker_socket:
            raise ValueError(
                "mounting the host Docker socket is forbidden: it grants the sandbox "
                "control over the host (spec section 35)"
            )
        if self.allow_privileged:
            raise ValueError("privileged sandboxes are forbidden (spec section 35)")
        if self.network_mode is NetworkMode.ALLOWLIST and not self.allowed_hosts:
            raise ValueError("ALLOWLIST network mode requires at least one allowed host")
        if self.network_mode is NetworkMode.NONE and self.allowed_hosts:
            raise ValueError("allowed_hosts is meaningless when network_mode is NONE")
        return self

    def assert_environment_is_safe(self, environment: dict[str, str]) -> None:
        """Raise if ``environment`` would leak control-plane configuration.

        Called by every sandbox backend immediately before execution, so the check
        cannot be forgotten at a call site.
        """
        from scout_domain.errors import PolicyViolation

        leaked = sorted(set(environment) & set(self.forbidden_environment))
        if leaked:
            raise PolicyViolation(
                "refusing to expose control-plane environment variables to repository code",
                leaked=leaked,
            )


class SandboxRun(ScoutModel):
    """One sandbox instance and the commands executed inside it."""

    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    backend: SandboxBackendKind

    #: Container image or, for the local backend, the host Python version.
    image: str | None = Field(default=None, max_length=500)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    policy: SandboxPolicy = Field(default_factory=SandboxPolicy)

    status: SandboxStatus = SandboxStatus.PENDING
    workspace_path: str = Field(default="", max_length=2048)

    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0.0)

    #: Tool run ids executed inside this sandbox, in order.
    tool_run_ids: list[str] = Field(default_factory=list)
    error: str | None = Field(default=None, max_length=8000)
    #: Container id or process group id, recorded for troubleshooting leaks.
    native_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _validate(self) -> SandboxRun:
        if (
            self.status
            in {
                SandboxStatus.SUCCEEDED,
                SandboxStatus.FAILED,
                SandboxStatus.TIMEOUT,
                SandboxStatus.ERROR,
                SandboxStatus.CANCELLED,
            }
            and self.completed_at is None
        ):
            raise ValueError(f"a {self.status.value} sandbox run must record completed_at")
        if self.status is SandboxStatus.ERROR and not self.error:
            raise ValueError("an ERROR sandbox run must record an error")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            SandboxStatus.SUCCEEDED,
            SandboxStatus.FAILED,
            SandboxStatus.TIMEOUT,
            SandboxStatus.ERROR,
            SandboxStatus.CANCELLED,
        }
