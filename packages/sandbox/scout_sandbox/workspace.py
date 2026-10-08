"""Workspaces: the filesystem boundary for untrusted code (spec section 13).

A workspace is a directory that:

* lives under a configured root, never in the repository or the process's own tree;
* is created fresh for each job, so one job can never observe another's checkout;
* is deleted on completion unless retention is requested for debugging;
* resolves every caller-supplied relative path **inside itself**, rejecting absolute
  paths, ``..`` traversal, and symlinks that escape.

That last point is the security control this module exists for. Repository code, a
repository-supplied ``.scout/engineering.yaml``, and tool arguments all flow into path
construction. Any of them could otherwise be used to read ``~/.ssh/id_rsa`` or overwrite
a control-plane file.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from scout_domain.errors import PolicyViolation

__all__ = ["Workspace", "WorkspaceError"]


class WorkspaceError(PolicyViolation):
    """Raised when a workspace operation would escape its boundary or is otherwise unsafe."""


@dataclass(frozen=True, slots=True)
class WorkspaceLimits:
    """Ceilings enforced while populating a workspace."""

    #: Refuse to materialise a repository larger than this.
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    #: Refuse to materialise more files than this.
    max_files: int = 200_000


class Workspace:
    """An isolated, disposable directory for one job's repository checkout.

    Use as an async context manager so cleanup cannot be forgotten::

        async with Workspace.create(root, job_id) as workspace:
            await checkout(workspace)
    """

    def __init__(self, path: Path, *, job_id: str, retained: bool = False) -> None:
        self._path = path
        self.job_id = job_id
        #: When true the directory survives ``close`` for post-mortem inspection.
        self.retained = retained
        self._closed = False

    # -- Lifecycle -------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        root: Path | str,
        *,
        job_id: str,
        retain: bool = False,
    ) -> Workspace:
        """Create a fresh workspace directory under ``root``.

        The directory is created with restrictive permissions (``0o700``) so that a
        multi-user host cannot read another user's checkout.
        """
        root_path = Path(root).expanduser().resolve()
        root_path.mkdir(parents=True, exist_ok=True)

        # A job id is used in a path, so it must be a single safe path segment.
        if not job_id or "/" in job_id or "\\" in job_id or job_id in {".", ".."}:
            raise WorkspaceError(f"unsafe job id for workspace path: {job_id!r}", job_id=job_id)

        path = root_path / job_id
        if path.exists():
            # Reclaiming a crashed job's directory is expected, but never merge into it:
            # stale files from a previous attempt would silently corrupt analysis.
            shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, mode=0o700)
        return cls(path, job_id=job_id, retained=retain)

    async def __aenter__(self) -> Workspace:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        self.close()

    def assert_within_limits(self, limits: WorkspaceLimits) -> None:
        """Raise if the populated workspace exceeds ``limits``.

        Called after checkout, before any tool runs: refusing an oversized repository up
        front is far cheaper than discovering it when the disk fills.
        """
        files = self.list_files(limit=limits.max_files + 1)
        if len(files) > limits.max_files:
            raise WorkspaceError(
                f"workspace contains more than {limits.max_files} files",
                job_id=self.job_id,
            )
        total = self.total_size_bytes()
        if total > limits.max_total_bytes:
            raise WorkspaceError(
                f"workspace is {total} bytes, exceeding the {limits.max_total_bytes} byte limit",
                job_id=self.job_id,
            )

    def close(self) -> None:
        """Remove the workspace unless it is marked for retention."""
        if self._closed:
            return
        self._closed = True
        if self.retained:
            return
        _make_tree_writable(self._path)
        shutil.rmtree(self._path, ignore_errors=True)

    # -- Accessors -------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """Absolute path to the workspace root."""
        if self._closed:
            raise WorkspaceError("workspace has been closed", job_id=self.job_id)
        return self._path

    @property
    def is_closed(self) -> bool:
        return self._closed

    def resolve(self, relative_path: str | Path) -> Path:
        """Resolve ``relative_path`` inside the workspace, or refuse.

        Rejects absolute paths outright and verifies that the fully resolved path (after
        following symlinks) is still inside the workspace. Both checks are necessary:
        the first catches naive escapes, the second catches a symlink planted by
        repository code pointing at ``/etc`` or the host home directory.
        """
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise WorkspaceError(
                f"absolute paths are not permitted inside a sandbox workspace: {relative_path!r}",
                job_id=self.job_id,
            )
        if any(part == ".." for part in candidate.parts):
            raise WorkspaceError(
                f"path traversal is not permitted: {relative_path!r}",
                job_id=self.job_id,
            )

        root = self._path.resolve()
        resolved = (root / candidate).resolve()
        if resolved != root and root not in resolved.parents:
            raise WorkspaceError(
                f"path escapes the sandbox workspace: {relative_path!r} resolved to {resolved}",
                job_id=self.job_id,
            )
        return resolved

    # -- File operations -------------------------------------------------------------

    def write_text(self, relative_path: str, content: str, *, encoding: str = "utf-8") -> Path:
        """Write text to a path inside the workspace, creating parent directories."""
        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)
        return target

    def write_bytes(self, relative_path: str, content: bytes) -> Path:
        """Write bytes to a path inside the workspace, creating parent directories."""
        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def read_text(self, relative_path: str, *, max_bytes: int = 1_000_000) -> str:
        """Read a file from the workspace, refusing to load more than ``max_bytes``.

        The cap protects the worker's memory: repository code can produce a 5 GB log file,
        and reading it whole would kill the process rather than the job.
        """
        target = self.resolve(relative_path)
        size = target.stat().st_size
        if size > max_bytes:
            raise WorkspaceError(
                f"refusing to read {relative_path!r}: {size} bytes exceeds the "
                f"{max_bytes} byte limit",
                job_id=self.job_id,
            )
        return target.read_text(encoding="utf-8", errors="replace")

    def exists(self, relative_path: str) -> bool:
        try:
            return self.resolve(relative_path).exists()
        except WorkspaceError:
            return False

    def list_files(
        self,
        *,
        relative_to: str = ".",
        limit: int = 50_000,
        skip_directories: frozenset[str] | None = None,
    ) -> list[str]:
        """List regular files under ``relative_to`` as workspace-relative POSIX paths.

        Skips VCS and cache directories by default; they are large, irrelevant, and
        frequently contain absolute host paths that confuse downstream analysis.
        """
        skip = skip_directories or frozenset(
            {
                ".git",
                "node_modules",
                "__pycache__",
                ".venv",
                "venv",
                ".mypy_cache",
                ".ruff_cache",
                ".pytest_cache",
                "dist",
                "build",
                ".next",
            }
        )
        base = self.resolve(relative_to)
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if name not in skip]
            for filename in filenames:
                full = Path(dirpath) / filename
                if full.is_symlink():
                    continue
                if len(found) >= limit:
                    return found
                found.append(full.relative_to(self._path).as_posix())
        return sorted(found)

    def total_size_bytes(self, *, limit: int = 1_000_000) -> int:
        """Sum the size of files in the workspace, stopping early at ``limit`` files."""
        total = 0
        counted = 0
        for dirpath, dirnames, filenames in os.walk(self._path):
            dirnames[:] = [name for name in dirnames if name != ".git"]
            for filename in filenames:
                full = Path(dirpath) / filename
                if full.is_symlink():
                    continue
                try:
                    total += full.stat().st_size
                except OSError:
                    continue
                counted += 1
                if counted >= limit:
                    return total
        return total


def _make_tree_writable(path: Path) -> None:
    """Restore owner write/execute permissions so a tree can be deleted.

    Repository code can create read-only directories. Without this, cleanup silently
    fails and the workspace root fills up, which is a slow-motion outage.
    """
    if not path.exists():
        return
    for dirpath, dirnames, filenames in os.walk(path):
        for name in (*dirnames, *filenames):
            target = Path(dirpath) / name
            try:
                if target.is_symlink():
                    continue
                target.chmod(target.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
            except OSError:
                continue
