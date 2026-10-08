"""Safe Git operations for materialising a pinned revision (spec section 13).

Spec section 13 is unambiguous: **never analyse an unpinned moving branch.** This module
exists to make that easy to comply with and hard to get wrong.

Two security decisions are worth calling out:

* **No shell.** Every git invocation is an argv list. Paths, refs, and SHAs coming from a
  webhook are untrusted input.
* **No persisted credentials.** A GitHub installation token is passed as a per-command
  ``-c http.extraheader=...`` argument rather than being written into ``.git/config``,
  where repository code could read it. ``credential.helper`` is disabled so no keychain or
  on-disk credential store is consulted.

Errors are classified for retry: a network failure fetching from GitHub is retryable, a
missing commit or an authentication failure is not.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from scout_domain.enums import FailureClass
from scout_domain.errors import ScoutDomainError
from scout_domain.state.retry import register_failure_class
from scout_observability import get_logger, instrument_span
from scout_sandbox.base import Sandbox
from scout_sandbox.command import CommandOutcome

if TYPE_CHECKING:
    from scout_sandbox.workspace import Workspace

__all__ = [
    "CheckoutResult",
    "FileChange",
    "GitCommandError",
    "GitError",
    "GitNotAvailable",
    "GitRefNotFound",
    "GitRunner",
    "GitTransportError",
]

_log = get_logger(__name__)

#: Git refs and SHAs coming from a webhook are validated before use. Git's own parsing is
#: permissive, and a value beginning with ``-`` would be read as an option.
_SHA_PATTERN = re.compile(r"^[0-9a-f]{7,64}$")
_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/\-]{1,255}$")

#: Shallow-clone depth. Two commits are all a diff needs.
DEFAULT_FETCH_DEPTH = 2


class GitError(ScoutDomainError):
    """Base class for git failures."""

    failure_class: FailureClass = FailureClass.NON_RETRYABLE


class GitNotAvailable(GitError):
    """The git binary is missing from the sandbox."""

    failure_class = FailureClass.NON_RETRYABLE


class GitTransportError(GitError):
    """A network or remote failure occurred while talking to the remote."""

    failure_class = FailureClass.RETRYABLE


class GitRefNotFound(GitError):
    """The requested commit or ref does not exist on the remote."""

    failure_class = FailureClass.NON_RETRYABLE


class GitCommandError(GitError):
    """A git command failed and the cause is not transport-related."""

    failure_class = FailureClass.NON_RETRYABLE


for _error in (GitError, GitNotAvailable, GitTransportError, GitRefNotFound, GitCommandError):
    register_failure_class(_error, _error.failure_class)
del _error


@dataclass(frozen=True, slots=True)
class FileChange:
    """One entry from ``git diff --name-status``."""

    status: str
    path: str
    previous_path: str | None = None


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """What was materialised into the workspace."""

    head_sha: str
    base_sha: str
    merge_base_sha: str | None
    shallow: bool
    file_count: int


def validate_sha(value: str) -> str:
    """Validate a commit SHA before it reaches git."""
    if not _SHA_PATTERN.match(value or ""):
        raise GitError(f"not a valid commit SHA: {value!r}", value=value)
    return value


def validate_ref(value: str) -> str:
    """Validate a branch or tag ref before it reaches git."""
    if not _REF_PATTERN.match(value or "") or value.startswith("-") or ".." in value:
        raise GitError(f"not a valid git ref: {value!r}", value=value)
    return value


class GitRunner:
    """Runs git inside a sandbox workspace.

    Git runs *inside* the sandbox so that a malformed repository cannot influence the
    host. On the local backend that is a weaker guarantee, which is another reason the
    Docker backend is the production default.
    """

    def __init__(
        self, sandbox: Sandbox, *, git_binary: str = "git", depth: int = DEFAULT_FETCH_DEPTH
    ) -> None:
        self._sandbox = sandbox
        self.git_binary = git_binary
        self.depth = depth

    # -- Remote configuration --------------------------------------------------------

    def _base_argv(self, *, token: str | None = None) -> list[str]:
        """Build the git prefix, including per-command transport configuration.

        The authorization header is supplied per command so it is never written to
        ``.git/config``. ``credential.helper`` is cleared so git cannot fall back to a
        stored credential. ``GIT_TERMINAL_PROMPT=0`` (set by the sandbox environment)
        prevents an interactive prompt from hanging a worker forever.
        """
        argv = [
            self.git_binary,
            "-c",
            "credential.helper=",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "advice.detachedHead=false",
            "-c",
            "protocol.version=2",
        ]
        if token:
            encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            argv.extend(["-c", f"http.extraheader=AUTHORIZATION: basic {encoded}"])
        return argv

    # -- Workspace preparation -------------------------------------------------------

    async def init(self, workspace: Workspace, *, repo_url: str, token: str | None = None) -> None:
        """Initialise an empty repository and point ``origin`` at the remote."""
        await self._run(
            workspace,
            [*self._base_argv(token=token), "init", "--quiet", "--initial-branch=scout-verify"],
            allow_failure=False,
        )
        await self._run(
            workspace,
            [*self._base_argv(token=token), "remote", "add", "origin", repo_url],
            allow_failure=False,
        )

    async def fetch_commit(
        self, workspace: Workspace, sha: str, *, token: str | None = None
    ) -> None:
        """Fetch one specific commit, shallowly.

        Fetching by SHA is what allows a pinned verification of exactly the commit under
        review without cloning an entire history — which matters because the initial
        deployment target has limited disk and bandwidth (ADR 0001).
        """
        validate_sha(sha)
        result = await self._run(
            workspace,
            [
                *self._base_argv(token=token),
                "fetch",
                "--quiet",
                "--no-tags",
                f"--depth={self.depth}",
                "origin",
                sha,
            ],
            allow_failure=True,
        )
        if result.exit_code != 0:
            stderr = result.read_stderr()
            if _is_transport_failure(stderr):
                raise GitTransportError(
                    f"failed to fetch {sha} from the remote", sha=sha, stderr=stderr[:1000]
                )
            raise GitRefNotFound(
                f"commit {sha} is not available on the remote; it may have been force-pushed",
                sha=sha,
                stderr=stderr[:500],
            )

    async def checkout(self, workspace: Workspace, sha: str) -> None:
        """Force-checkout ``sha`` into the working tree."""
        validate_sha(sha)
        await self._run(
            workspace,
            [self.git_binary, "checkout", "--quiet", "--force", "--detach", sha],
            allow_failure=False,
        )

    async def materialize(
        self,
        workspace: Workspace,
        *,
        repo_url: str,
        base_sha: str,
        head_sha: str,
        token: str | None = None,
    ) -> CheckoutResult:
        """Fetch and check out the exact revision to verify.

        Also fetches the base commit so a correct three-dot diff is possible, and records
        the merge base. The returned SHAs are read back from git rather than trusted from
        the webhook: GitHub tells Scout what it *should* be, and git confirms it.
        """
        base = validate_sha(base_sha)
        head = validate_sha(head_sha)

        with instrument_span(
            "sandbox.git.materialize",
            attributes={"git.base_sha": base, "git.head_sha": head},
        ):
            await self.init(workspace, repo_url=repo_url, token=token)
            await self.fetch_commit(workspace, base, token=token)
            if head != base:
                await self.fetch_commit(workspace, head, token=token)
            await self.checkout(workspace, head)

        actual_head = (await self.rev_parse(workspace, "HEAD")) or head
        if not actual_head.startswith(head[:12]) and not head.startswith(actual_head[:12]):
            # A mismatch means the working tree does not match the approved revision.
            # That must fail loudly, not be logged and ignored.
            raise GitCommandError(
                "checked-out commit does not match the requested head SHA",
                requested=head,
                actual=actual_head,
            )

        merge_base = await self.merge_base(workspace, base, actual_head)
        files = await self.diff_name_status(workspace, base, actual_head)
        return CheckoutResult(
            head_sha=actual_head,
            base_sha=base,
            merge_base_sha=merge_base,
            shallow=True,
            file_count=len(files),
        )

    # -- Inspection ------------------------------------------------------------------

    async def rev_parse(self, workspace: Workspace, ref: str) -> str | None:
        outcome = await self._run(
            workspace,
            [self.git_binary, "rev-parse", "--verify", f"{ref}^{{commit}}"],
            allow_failure=True,
        )
        if outcome.exit_code != 0:
            return None
        return outcome.read_stdout(limit_bytes=200).strip() or None

    async def merge_base(self, workspace: Workspace, first: str, second: str) -> str | None:
        validate_sha(first)
        validate_sha(second)
        outcome = await self._run(
            workspace,
            [self.git_binary, "merge-base", first, second],
            allow_failure=True,
        )
        if outcome.exit_code != 0:
            return None
        return outcome.read_stdout(limit_bytes=200).strip() or None

    async def diff_name_status(
        self, workspace: Workspace, base_sha: str, head_sha: str
    ) -> list[FileChange]:
        """Return the changed files between two commits, including renames.

        ``-M`` enables rename detection; ``--no-renames`` would report a rename as a
        delete plus an add, which produces two misleading entries instead of one accurate
        one.
        """
        validate_sha(base_sha)
        validate_sha(head_sha)
        outcome = await self._run(
            workspace,
            [
                self.git_binary,
                "diff",
                "--name-status",
                "-M",
                "--no-color",
                "--no-ext-diff",
                f"{base_sha}...{head_sha}",
            ],
            allow_failure=False,
        )
        changes: list[FileChange] = []
        for line in outcome.read_stdout(limit_bytes=8_000_000).splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0].strip()
            if status.startswith("R") and len(parts) >= 3:
                changes.append(FileChange(status=status, path=parts[2], previous_path=parts[1]))
            else:
                changes.append(FileChange(status=status, path=parts[1]))
        return changes

    async def diff(self, workspace: Workspace, base_sha: str, head_sha: str) -> str:
        """Return the unified diff for the change.

        This is stored verbatim as an artifact, so flags that would alter the output
        (``--stat``, ``--color``, external diff drivers) are explicitly disabled.
        """
        validate_sha(base_sha)
        validate_sha(head_sha)
        outcome = await self._run(
            workspace,
            [
                self.git_binary,
                "diff",
                "-M",
                "--no-color",
                "--no-ext-diff",
                "--no-prefix",
                "--unified=3",
                f"{base_sha}...{head_sha}",
            ],
            allow_failure=False,
        )
        return outcome.read_stdout(limit_bytes=64 * 1024 * 1024)

    async def show_file(self, workspace: Workspace, sha: str, path: str) -> str | None:
        """Return a file's contents at a specific commit, or ``None`` if absent.

        Reading from a pinned commit (rather than the working tree) means a neighbouring
        file passed to an agent is the version that the change was written against.
        """
        validate_sha(sha)
        # `--` terminates option parsing so a path beginning with `-` is treated as a path.
        outcome = await self._run(
            workspace,
            [self.git_binary, "show", f"{sha}:{path}"],
            allow_failure=True,
        )
        if outcome.exit_code != 0:
            return None
        return outcome.read_stdout(limit_bytes=2_000_000)

    async def log(
        self, workspace: Workspace, base_sha: str, head_sha: str, *, limit: int = 50
    ) -> list[str]:
        """Return commit subjects in the range, used as requirement material."""
        validate_sha(base_sha)
        validate_sha(head_sha)
        outcome = await self._run(
            workspace,
            [
                self.git_binary,
                "log",
                "--no-color",
                f"--max-count={limit}",
                "--pretty=format:%s",
                f"{base_sha}..{head_sha}",
            ],
            allow_failure=True,
        )
        if outcome.exit_code != 0:
            return []
        return [line for line in outcome.read_stdout().splitlines() if line.strip()]

    async def ls_files(self, workspace: Workspace) -> list[str]:
        """List tracked files at the current revision."""
        outcome = await self._run(workspace, [self.git_binary, "ls-files"], allow_failure=False)
        return [line for line in outcome.read_stdout().splitlines() if line.strip()]

    # -- Internals -------------------------------------------------------------------

    async def _run(
        self, workspace: Workspace, argv: list[str], *, allow_failure: bool
    ) -> CommandOutcome:
        """Execute a git command in the workspace.

        ``allow_failure`` exists so that callers can distinguish "git said no, and that
        is information" (``merge-base`` on unrelated histories) from "git said no, and
        that is a failure" (``checkout`` failing after a successful fetch).
        """
        # Fail fast with a clear domain error if the workspace was already torn down,
        # rather than surfacing "path escapes the sandbox workspace" from deep inside the
        # backend. `resolve(".")` is the cheap validity probe for that.
        workspace.resolve(".")

        outcome = await self._sandbox.run_simple(
            *argv,
            cwd=".",
            env={"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"},
            label=f"git:{argv[1] if len(argv) > 1 else 'cmd'}",
        )
        if not allow_failure and not outcome.succeeded:
            stderr = outcome.read_stderr(limit_bytes=4000)
            if _is_transport_failure(stderr):
                raise GitTransportError(
                    f"git {argv[1] if len(argv) > 1 else ''} failed: {stderr[:500]}",
                    argv=argv[:8],
                )
            raise GitCommandError(
                f"git {argv[1] if len(argv) > 1 else ''} failed with exit "
                f"{outcome.exit_code}: {stderr[:500]}",
                exit_code=outcome.exit_code,
            )
        return outcome


def _is_transport_failure(stderr: str) -> bool:
    """Classify a git stderr message as a transport failure (retryable) or not.

    Retry classification matters here: a DNS blip while fetching from GitHub should be
    retried, but a missing commit should not be -- retrying it just burns the budget
    (spec section 51).
    """
    lowered = stderr.lower()
    return any(
        marker in lowered
        for marker in (
            "could not resolve host",
            "connection timed out",
            "connection reset",
            "operation timed out",
            "temporary failure in name resolution",
            "the remote end hung up",
            "rpc failed",
            "early eof",
            "ssl",
            "unable to access",
        )
    )


async def ensure_git_available(sandbox: Sandbox) -> None:
    """Raise :class:`GitNotAvailable` when the sandbox has no git binary."""
    resolved = await sandbox.which("git")
    if resolved is None:
        raise GitNotAvailable("git is not available inside the sandbox image")
    _log.debug("git resolved inside sandbox", path=resolved)


def repo_url_for(full_name: str, *, base_url: str = "https://github.com") -> str:
    """Build the canonical HTTPS clone URL for a repository."""
    return f"{base_url.rstrip('/')}/{full_name}.git"
