"""Command execution contracts for the sandbox layer.

Output is streamed to files rather than captured into memory. Repository code can emit
arbitrarily large output, and a tool that buffers a 2 GB test log into a Python string
takes down the worker instead of failing one job. :class:`CommandOutcome` therefore holds
*paths and sizes*, and callers read a bounded head or tail when they need the text.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "DEFAULT_CAPTURE_LIMIT_BYTES",
    "CommandOutcome",
    "SandboxCommand",
    "tail_file",
]

#: Maximum bytes captured per stream before truncation. Raw evidence is still preserved
#: up to this bound; the truncation is recorded explicitly rather than silently applied.
DEFAULT_CAPTURE_LIMIT_BYTES: int = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    """A fully-resolved command to execute inside a sandbox.

    ``argv`` is a list, never a string. There is no shell anywhere in Scout's execution
    path: no ``shell=True``, no ``sh -c``, no string interpolation of repository input.
    That single decision removes command injection as a vulnerability class.
    """

    argv: Sequence[str]
    cwd: str = "."
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = 600
    stdin: bytes | None = None
    #: Human-facing label used in artifacts and logs, e.g. ``pytest``.
    label: str = ""
    #: Per-stream capture ceiling for this command.
    capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("argv must not be empty")
        if any(not isinstance(part, str) for part in self.argv):
            raise ValueError("every argv element must be a string")
        if "\x00" in " ".join(self.argv):
            raise ValueError("argv must not contain NUL bytes")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

    @property
    def display(self) -> str:
        """Render the command for logs and artifacts.

        Quoting is for display only; the executed argv is always the list form.
        """
        return " ".join(_quote(part) for part in self.argv)

    @property
    def executable(self) -> str:
        return str(self.argv[0])


def _quote(part: str) -> str:
    if part and all(character.isalnum() or character in "-_./=:@," for character in part):
        return part
    return "'" + part.replace("'", "'\\''") + "'"


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """The result of one sandboxed command execution."""

    argv: tuple[str, ...]
    exit_code: int | None
    #: Signal number when the process was killed by a signal, else ``None``.
    signal: int | None = None
    timed_out: bool = False

    stdout_path: Path | None = None
    stderr_path: Path | None = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float = 0.0

    #: Set when the command never started (missing binary, permission denied).
    launch_error: str | None = None
    sandbox_run_id: str | None = None

    @property
    def launched(self) -> bool:
        return self.launch_error is None

    @property
    def succeeded(self) -> bool:
        """Whether the command ran and exited zero."""
        return self.launch_error is None and self.exit_code == 0 and not self.timed_out

    @property
    def was_killed_by_signal(self) -> bool:
        return self.signal is not None

    @property
    def likely_memory_kill(self) -> bool:
        """Whether the process was likely killed for exceeding the memory ceiling.

        SIGKILL (9) with no exit code and no timeout is the signature of an OOM kill on
        both Linux (cgroup) and macOS. Reporting it as a code defect would be wrong.
        """
        return self.signal == 9 and not self.timed_out

    def read_stdout(self, *, limit_bytes: int = 200_000) -> str:
        """Read up to ``limit_bytes`` from the head of stdout."""
        return _read_bounded(self.stdout_path, limit_bytes)

    def read_stderr(self, *, limit_bytes: int = 200_000) -> str:
        """Read up to ``limit_bytes`` from the head of stderr."""
        return _read_bounded(self.stderr_path, limit_bytes)

    def read_stdout_tail(self, *, limit_bytes: int = 20_000) -> str:
        """Read the tail of stdout, which is where test-failure summaries appear."""
        return tail_file(self.stdout_path, limit_bytes=limit_bytes)

    def read_stderr_tail(self, *, limit_bytes: int = 20_000) -> str:
        return tail_file(self.stderr_path, limit_bytes=limit_bytes)

    def describe_failure(self) -> str:
        """A one-line, human-readable explanation suitable for a report or a log."""
        if self.launch_error:
            return f"command could not be started: {self.launch_error}"
        if self.timed_out:
            return f"command exceeded its timeout after {self.duration_seconds:.1f}s"
        if self.likely_memory_kill:
            return "command was killed, most likely for exceeding the memory limit"
        if self.signal is not None:
            return f"command was terminated by signal {self.signal}"
        return f"command exited with status {self.exit_code}"


def _read_bounded(path: Path | None, limit_bytes: int) -> str:
    """Read at most ``limit_bytes`` from the start of a file, decoding leniently."""
    if path is None or not path.exists():
        return ""
    try:
        with path.open("rb") as handle:
            data = handle.read(limit_bytes)
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def tail_file(path: Path | None, *, limit_bytes: int = 20_000) -> str:
    """Read the last ``limit_bytes`` of a file.

    Test runners print their failure summary at the end, so the tail is usually the part
    a reviewer needs. Seeking from the end keeps this cheap even for enormous logs.
    """
    if path is None or not path.exists():
        return ""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit_bytes:
                handle.seek(size - limit_bytes)
            data = handle.read(limit_bytes)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if size > limit_bytes:
        return f"<... truncated, showing last {limit_bytes} bytes ...>\n{text}"
    return text


def resolve_environment(
    base: Mapping[str, str],
    overrides: Mapping[str, str],
    *,
    forbidden: frozenset[str],
) -> dict[str, str]:
    """Merge ``base`` and ``overrides``, refusing to pass any forbidden variable.

    ``PATH`` and locale are inherited from a minimal allowlist rather than the parent
    environment, because inheriting the parent environment is how control-plane
    credentials leak into untrusted code (spec section 53).
    """
    merged: dict[str, str] = {}
    for key, value in (*base.items(), *overrides.items()):
        if key in forbidden:
            from scout_domain.errors import PolicyViolation

            raise PolicyViolation(
                "refusing to pass a forbidden environment variable into the sandbox",
                variable=key,
            )
        merged[key] = value
    return merged


#: Environment variables a sandboxed process may inherit from Scout's own environment.
#: Everything else is dropped, so an accidental ``os.environ`` copy cannot leak secrets.
PASSTHROUGH_ENVIRONMENT: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMPDIR",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
    }
)


def build_minimal_environment(
    *,
    extra: Mapping[str, str] | None = None,
    source: Mapping[str, str] | None = None,
    forbidden: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Build a minimal, allowlisted environment for a sandboxed command."""
    origin = source if source is not None else os.environ
    environment = {key: origin[key] for key in PASSTHROUGH_ENVIRONMENT if key in origin}
    environment.setdefault("PYTHONUNBUFFERED", "1")
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    # Deterministic tool output: no colour codes, no progress bars, no pagers.
    environment.setdefault("NO_COLOR", "1")
    environment.setdefault("CI", "1")
    environment.setdefault("TERM", "dumb")
    environment.setdefault("GIT_TERMINAL_PROMPT", "0")
    if extra:
        for key, value in extra.items():
            if key in forbidden:
                from scout_domain.errors import PolicyViolation

                raise PolicyViolation(
                    "refusing to pass a forbidden environment variable into the sandbox",
                    variable=key,
                )
            environment[key] = value
    return environment
