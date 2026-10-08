"""The artifact store: where raw evidence actually lives (Rule 15, spec section 21).

Design: **content-addressed**. An artifact's storage key is derived from the SHA-256 of
its bytes. Three things follow, all of them useful:

* The same output stored twice costs one blob. Re-running an identical test suite creates
  a new artifact *row* (so the timeline is complete) but not a new file on disk.
* Integrity is verifiable: re-hashing the bytes must reproduce the key, which
  :meth:`LocalArtifactStore.verify` checks.
* Retention can be reasoned about by size rather than by row count.

The database stores :class:`~scout_domain.models.artifact.Artifact` metadata; this module
stores bytes. Findings reference artifact ids, so the chain from a report claim to the raw
output that produced it is unbroken.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from scout_domain.errors import ScoutDomainError
from scout_domain.ids import IdKind, new_id
from scout_domain.models.artifact import Artifact as ArtifactRecord
from scout_domain.models.artifact import ArtifactKind
from scout_observability import get_logger, instrument_span

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

__all__ = [
    "ArtifactStore",
    "InMemoryArtifactStore",
    "LocalArtifactStore",
    "S3ArtifactStoreStub",
    "content_address",
]

_log = get_logger(__name__)

#: Read/write chunk size for streaming. 1 MiB balances syscall count against memory.
_CHUNK_SIZE = 1024 * 1024


def content_address(data: bytes) -> str:
    """Return the hex SHA-256 of ``data``, Scout's content address."""
    return hashlib.sha256(data).hexdigest()


@runtime_checkable
class ArtifactStore(Protocol):
    """Storage for raw evidence.

    A protocol rather than an abstract base class so that a cloud implementation
    (S3, GCS) can be added in the Phase 3 evolution described in the architecture
    document without touching a single caller.
    """

    def put_bytes(
        self,
        data: bytes,
        *,
        kind: ArtifactKind,
        content_type: str = "text/plain",
        filename: str | None = None,
        job_id: str | None = None,
        tool_run_id: str | None = None,
        agent_run_id: str | None = None,
        sandbox_run_id: str | None = None,
    ) -> ArtifactRecord:
        """Persist ``data`` and return its metadata record."""
        ...

    def put_text(self, text: str, **kwargs: object) -> ArtifactRecord:
        """Persist UTF-8 text."""
        ...

    def get_bytes(self, artifact: ArtifactRecord) -> bytes:
        """Return the stored bytes, verifying integrity."""
        ...

    def exists(self, artifact: ArtifactRecord) -> bool:
        """Whether the bytes are still present."""
        ...

    def delete(self, artifact: ArtifactRecord) -> bool:
        """Remove the bytes. Returns whether anything was removed."""
        ...


class LocalArtifactStore:
    """Filesystem artifact store rooted at ``root``.

    Layout::

        <root>/sha256/ab/cd/abcdef...   # two-level fan-out keeps directories small

    Writes are atomic (temp file plus ``os.replace``), so a worker killed mid-write can
    never leave a partially written blob that would later fail an integrity check and be
    mistaken for corruption.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._blob_root = self.root / "sha256"
        self._tmp_root = self.root / ".incoming"
        self._tmp_root.mkdir(parents=True, exist_ok=True)

    # -- Paths -----------------------------------------------------------------------

    def storage_key(self, digest: str) -> str:
        """Return the relative storage key for a digest."""
        return f"sha256/{digest[:2]}/{digest[2:4]}/{digest}"

    def path_for_digest(self, digest: str) -> Path:
        """Return the absolute path for a digest."""
        return self.root / self.storage_key(digest)

    # -- Writes ----------------------------------------------------------------------

    def put_bytes(
        self,
        data: bytes,
        *,
        kind: ArtifactKind,
        content_type: str = "text/plain",
        filename: str | None = None,
        job_id: str | None = None,
        tool_run_id: str | None = None,
        agent_run_id: str | None = None,
        sandbox_run_id: str | None = None,
    ) -> ArtifactRecord:
        digest = content_address(data)
        target = self.path_for_digest(digest)

        with instrument_span(
            "artifacts.put",
            attributes={
                "artifact.kind": kind.value,
                "artifact.size_bytes": len(data),
                "artifact.digest": digest[:12],
                "job.id": job_id,
            },
        ):
            if not target.exists():
                self._write_atomically(target, data)
            else:
                _log.debug("artifact already present, deduplicated", digest=digest[:12])

        return ArtifactRecord(
            id=new_id(IdKind.ARTIFACT),
            job_id=job_id,
            kind=kind,
            content_type=content_type,
            size_bytes=len(data),
            sha256=digest,
            storage_key=self.storage_key(digest),
            filename=filename,
            tool_run_id=tool_run_id,
            agent_run_id=agent_run_id,
            sandbox_run_id=sandbox_run_id,
        )

    def put_text(self, text: str, **kwargs: object) -> ArtifactRecord:
        return self.put_bytes(text.encode("utf-8"), **kwargs)  # type: ignore[arg-type]

    def put_file(
        self,
        source: Path,
        *,
        kind: ArtifactKind,
        content_type: str = "text/plain",
        filename: str | None = None,
        job_id: str | None = None,
        tool_run_id: str | None = None,
        agent_run_id: str | None = None,
        sandbox_run_id: str | None = None,
    ) -> ArtifactRecord:
        """Store an existing file by streaming it.

        Used for tool stdout/stderr captures, which may be hundreds of megabytes and must
        never be read into memory just to be stored.
        """
        if not source.exists():
            raise FileNotFoundError(f"artifact source does not exist: {source}")

        digest = self._hash_file(source)
        target = self.path_for_digest(digest)
        size = source.stat().st_size

        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self._tmp_root, delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    with source.open("rb") as reader:
                        shutil.copyfileobj(reader, handle, _CHUNK_SIZE)
                    handle.flush()
                    os.fsync(handle.fileno())
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
            temporary.replace(target)

        return ArtifactRecord(
            id=new_id(IdKind.ARTIFACT),
            job_id=job_id,
            kind=kind,
            content_type=content_type,
            size_bytes=size,
            sha256=digest,
            storage_key=self.storage_key(digest),
            filename=filename or source.name,
            tool_run_id=tool_run_id,
            agent_run_id=agent_run_id,
            sandbox_run_id=sandbox_run_id,
        )

    # -- Reads -----------------------------------------------------------------------

    def get_bytes(self, artifact: ArtifactRecord) -> bytes:
        path = self.root / artifact.storage_key
        if not path.exists():
            raise FileNotFoundError(
                f"artifact {artifact.id} is recorded but its bytes are missing: {path}"
            )
        data = path.read_bytes()
        actual = content_address(data)
        if actual != artifact.sha256:
            raise ArtifactIntegrityError(artifact.id, expected=artifact.sha256, actual=actual)
        return data

    def get_text(self, artifact: ArtifactRecord) -> str:
        return self.get_bytes(artifact).decode("utf-8", errors="replace")

    def open_stream(self, artifact: ArtifactRecord) -> Iterator[bytes]:
        """Yield the stored bytes in chunks, for large artifacts."""
        path = self.root / artifact.storage_key
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                yield chunk

    def exists(self, artifact: ArtifactRecord) -> bool:
        return (self.root / artifact.storage_key).exists()

    def verify(self, artifact: ArtifactRecord) -> bool:
        """Re-hash the stored bytes and confirm they match the recorded digest."""
        path = self.root / artifact.storage_key
        if not path.exists():
            return False
        return self._hash_file(path) == artifact.sha256

    # -- Deletion / retention --------------------------------------------------------

    def delete(self, artifact: ArtifactRecord) -> bool:
        """Delete a blob.

        Callers should be aware that content addressing means a blob may be shared by
        several artifact rows. Deleting a blob that is still referenced elsewhere would
        break those records, so :meth:`prune_expired` is the safe retention entry point
        and this method is intended for test cleanup.
        """
        path = self.root / artifact.storage_key
        if not path.exists():
            return False
        path.unlink()
        return True

    def prune_expired(
        self,
        artifacts: Iterable[ArtifactRecord],
        *,
        now: datetime,
        shared_digests: frozenset[str] = frozenset(),
    ) -> int:
        """Delete blobs whose records have expired and which nothing else references.

        ``shared_digests`` is the set of digests still referenced by a live record; a
        blob in that set is never removed, which is what makes retention safe under
        deduplication.
        """
        removed = 0
        for artifact in artifacts:
            if artifact.expires_at is None or artifact.expires_at > now:
                continue
            if artifact.sha256 in shared_digests:
                continue
            if self.delete(artifact):
                removed += 1
        return removed

    def total_bytes(self) -> int:
        """Total size of stored blobs, for capacity monitoring."""
        total = 0
        if not self._blob_root.exists():
            return 0
        for path in self._blob_root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    # -- Internals -------------------------------------------------------------------

    def _write_atomically(self, target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=self._tmp_root, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        temporary.replace(target)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest()


class InMemoryArtifactStore:
    """Artifact store backed by a dictionary. Used by tests and by dry runs."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put_bytes(
        self,
        data: bytes,
        *,
        kind: ArtifactKind,
        content_type: str = "text/plain",
        filename: str | None = None,
        job_id: str | None = None,
        tool_run_id: str | None = None,
        agent_run_id: str | None = None,
        sandbox_run_id: str | None = None,
    ) -> ArtifactRecord:
        digest = content_address(data)
        self._blobs[digest] = data
        return ArtifactRecord(
            id=new_id(IdKind.ARTIFACT),
            job_id=job_id,
            kind=kind,
            content_type=content_type,
            size_bytes=len(data),
            sha256=digest,
            storage_key=f"memory/{digest}",
            filename=filename,
            tool_run_id=tool_run_id,
            agent_run_id=agent_run_id,
            sandbox_run_id=sandbox_run_id,
        )

    def put_text(self, text: str, **kwargs: object) -> ArtifactRecord:
        return self.put_bytes(text.encode("utf-8"), **kwargs)  # type: ignore[arg-type]

    def put_file(self, source: Path, **kwargs: object) -> ArtifactRecord:
        return self.put_bytes(source.read_bytes(), **kwargs)  # type: ignore[arg-type]

    def get_bytes(self, artifact: ArtifactRecord) -> bytes:
        try:
            return self._blobs[artifact.sha256]
        except KeyError as exc:
            raise FileNotFoundError(
                f"artifact {artifact.id} is not present in this in-memory store"
            ) from exc

    def get_text(self, artifact: ArtifactRecord) -> str:
        return self.get_bytes(artifact).decode("utf-8", errors="replace")

    def exists(self, artifact: ArtifactRecord) -> bool:
        return artifact.sha256 in self._blobs

    def verify(self, artifact: ArtifactRecord) -> bool:
        return content_address(self.get_bytes(artifact)) == artifact.sha256

    def delete(self, artifact: ArtifactRecord) -> bool:
        return self._blobs.pop(artifact.sha256, None) is not None

    def total_bytes(self) -> int:
        return sum(len(blob) for blob in self._blobs.values())


class S3ArtifactStoreStub:
    """Placeholder documenting the Phase 3 migration path.

    The architecture document plans a move to object storage when Scout outgrows a single
    machine. Implementing it now would violate Rule 1 ("do not introduce infrastructure
    before it is necessary"), so this stub exists only to make the seam explicit and to
    fail loudly if something tries to use it before it is written.
    """

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(
            "S3 artifact storage is a Phase 3 item (see docs/ARCHITECTURE.md). "
            "Use LocalArtifactStore for now; the ArtifactStore protocol is the seam."
        )


class ArtifactIntegrityError(ScoutDomainError):
    """Raised when stored bytes do not match their recorded digest.

    Deliberately not a silent warning: evidence that cannot be trusted must not be presented
    as evidence. A domain error rather than a bare ``Exception`` so it flows through the same
    typed error handling as everything else and is never mistaken for an unexpected crash.
    """

    def __init__(self, artifact_id: str, *, expected: str, actual: str) -> None:
        super().__init__(
            f"artifact {artifact_id} failed its integrity check",
            expected=expected,
            actual=actual,
        )
