"""Isolated execution for untrusted repository code (spec sections 20, 35, 36, 53).

This package is the only place in Scout where untrusted code is launched. That
concentration is deliberate: it means the entire isolation story can be reviewed by
reading a handful of modules, and a change that weakens isolation has to happen here,
where a test can catch it.

Layout:

* :mod:`scout_sandbox.base` — the :class:`Sandbox` interface and its invariants.
* :mod:`scout_sandbox.workspace` — the filesystem boundary, including path-escape defence.
* :mod:`scout_sandbox.command` — the argv-only command contract and bounded output capture.
* :mod:`scout_sandbox.local` — host-process backend (development; not a security boundary).
* :mod:`scout_sandbox.docker` — container backend (production isolation boundary).
* :mod:`scout_sandbox.git` — pinned-revision checkout without persisting credentials.
* :mod:`scout_sandbox.factory` — backend, limits, and network policy selection.
"""

from __future__ import annotations

from scout_sandbox.base import Sandbox, validate_executable_name
from scout_sandbox.command import (
    DEFAULT_CAPTURE_LIMIT_BYTES,
    PASSTHROUGH_ENVIRONMENT,
    CommandOutcome,
    SandboxCommand,
    build_minimal_environment,
    resolve_environment,
    tail_file,
)
from scout_sandbox.docker import DockerSandbox
from scout_sandbox.errors import (
    SandboxBackendUnavailable,
    SandboxCommandError,
    SandboxError,
    SandboxLimitExceeded,
    SandboxTimeout,
)
from scout_sandbox.factory import (
    SANDBOX_IMAGE_ENV,
    build_policy,
    build_resource_limits,
    create_sandbox,
    resolve_backend,
    workspace_root,
)
from scout_sandbox.git import (
    CheckoutResult,
    FileChange,
    GitCommandError,
    GitError,
    GitNotAvailable,
    GitRefNotFound,
    GitRunner,
    GitTransportError,
    ensure_git_available,
    repo_url_for,
    validate_ref,
    validate_sha,
)
from scout_sandbox.local import LocalSandbox
from scout_sandbox.workspace import Workspace, WorkspaceError, WorkspaceLimits

__all__ = [
    "DEFAULT_CAPTURE_LIMIT_BYTES",
    "PASSTHROUGH_ENVIRONMENT",
    "SANDBOX_IMAGE_ENV",
    "CheckoutResult",
    "CommandOutcome",
    "DockerSandbox",
    "FileChange",
    "GitCommandError",
    "GitError",
    "GitNotAvailable",
    "GitRefNotFound",
    "GitRunner",
    "GitTransportError",
    "LocalSandbox",
    "Sandbox",
    "SandboxBackendUnavailable",
    "SandboxCommand",
    "SandboxCommandError",
    "SandboxError",
    "SandboxLimitExceeded",
    "SandboxTimeout",
    "Workspace",
    "WorkspaceError",
    "WorkspaceLimits",
    "build_minimal_environment",
    "build_policy",
    "build_resource_limits",
    "create_sandbox",
    "ensure_git_available",
    "repo_url_for",
    "resolve_backend",
    "resolve_environment",
    "tail_file",
    "validate_executable_name",
    "validate_ref",
    "validate_sha",
    "workspace_root",
]
