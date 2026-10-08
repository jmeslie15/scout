"""An artifact store that remembers every record it produced.

Why a wrapper rather than reading the rows back afterwards: a tool stores its stdout,
stderr, and machine-readable report *inside* :meth:`scout_tools.base.CommandTool.run`, and
the resulting :class:`~scout_domain.models.tool.ToolResult` keeps only the artifact *ids*.
The artifact table needs the digest, size, and storage key, which means the records have to
be captured at the moment they are created.

Re-reading them later is not an option either: the workspace (and therefore the capture
files) is torn down as soon as the stage finishes, which is deliberate.

The wrapper implements the :class:`scout_tools.artifacts.ArtifactStore` protocol, so a tool
cannot tell it apart from the real store.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, cast, runtime_checkable

from scout_domain.models.artifact import ArtifactKind

if TYPE_CHECKING:
    from scout_domain.models.artifact import Artifact
    from scout_tools.artifacts import ArtifactStore

__all__ = [
    "ANALYZING_ARTIFACT_FILENAME",
    "PREPARED_ARTIFACT_FILENAME",
    "RecordingArtifactStore",
    "WritableArtifactStore",
]

#: Filename of the prepared-state document PREPARING writes. Named so the resumption loader can
#: find it even though a job accumulates several ``REPOSITORY_SUMMARY`` artifacts.
PREPARED_ARTIFACT_FILENAME: Final = "prepared-state.json"

#: Filename of the architecture narrative ANALYZING writes. Named so the report builder quotes
#: the same text the stage produced, whether that text was model-assisted or deterministic.
ANALYZING_ARTIFACT_FILENAME: Final = "architecture-impact.md"


@runtime_checkable
class WritableArtifactStore(Protocol):
    """The full store interface Scout's tool layer actually uses.

    :class:`scout_tools.artifacts.ArtifactStore` omits ``put_file``, which every concrete
    store implements and which :meth:`scout_tools.base.Tool.store_artifact` looks up
    dynamically. Declaring it here keeps the wrapper typed without reaching for
    ``getattr``.
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
    ) -> Artifact:
        """Persist ``data`` and return its locator record."""
        ...

    def put_text(self, text: str, **kwargs: object) -> Artifact:
        """Persist UTF-8 text and return its locator record."""
        ...

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
    ) -> Artifact:
        """Stream an existing file into the store and return its locator record."""
        ...

    def get_bytes(self, artifact: Artifact) -> bytes:
        """Return the stored bytes, verifying integrity."""
        ...

    def exists(self, artifact: Artifact) -> bool:
        """Whether the bytes are still present."""
        ...

    def delete(self, artifact: Artifact) -> bool:
        """Remove the bytes, returning whether anything was removed."""
        ...


class RecordingArtifactStore:
    """Delegates to a real store and keeps every record it handed back."""

    def __init__(self, inner: ArtifactStore) -> None:
        """Wrap ``inner``.

        Typed against the narrow protocol rather than :class:`WritableArtifactStore` so a
        caller holding the platform's ``ArtifactStore`` can hand it over without a cast; the
        richer interface is asserted where it is used, in :meth:`put_file`.
        """
        self._inner = inner
        self._records: list[Artifact] = []

    @property
    def inner(self) -> ArtifactStore:
        """The wrapped store, for callers that need backend-specific helpers."""
        return self._inner

    @property
    def records(self) -> tuple[Artifact, ...]:
        """Every record produced so far, in creation order."""
        return tuple(self._records)

    def take(self) -> tuple[Artifact, ...]:
        """Return the records produced so far and forget them.

        Used by a stage that produced artifacts before a later step could fail: the caller
        still persists what was written, because evidence that exists must be recorded
        (Rule 15).
        """
        taken = tuple(self._records)
        self._records.clear()
        return taken

    def _remember(self, artifact: Artifact) -> Artifact:
        self._records.append(artifact)
        return artifact

    # -- ArtifactStore protocol ------------------------------------------------------

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
    ) -> Artifact:
        """Delegate, remembering the returned record."""
        return self._remember(
            self._inner.put_bytes(
                data,
                kind=kind,
                content_type=content_type,
                filename=filename,
                job_id=job_id,
                tool_run_id=tool_run_id,
                agent_run_id=agent_run_id,
                sandbox_run_id=sandbox_run_id,
            )
        )

    def put_text(self, text: str, **kwargs: object) -> Artifact:
        """Delegate, remembering the returned record."""
        return self._remember(self._inner.put_text(text, **kwargs))

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
    ) -> Artifact:
        """Delegate, remembering the returned record.

        Raises:
            NotImplementedError: when the wrapped store cannot stream a file. Every store
                Scout ships can; a future one that cannot must say so rather than silently
                dropping evidence.
        """
        delegate = cast("WritableArtifactStore", self._inner)
        return self._remember(
            delegate.put_file(
                source,
                kind=kind,
                content_type=content_type,
                filename=filename,
                job_id=job_id,
                tool_run_id=tool_run_id,
                agent_run_id=agent_run_id,
                sandbox_run_id=sandbox_run_id,
            )
        )

    def get_bytes(self, artifact: Artifact) -> bytes:
        """Read the bytes back through the wrapped store."""
        return self._inner.get_bytes(artifact)

    def exists(self, artifact: Artifact) -> bool:
        """Whether the wrapped store still holds the bytes."""
        return self._inner.exists(artifact)

    def delete(self, artifact: Artifact) -> bool:
        """Delete through the wrapped store."""
        return self._inner.delete(artifact)

    def __getattr__(self, name: str) -> object:
        """Expose backend-specific helpers such as ``verify`` and ``path_for_digest``.

        Only reached when the attribute is not found on this class, so the protocol methods
        above always win. The ``_inner`` name is excluded explicitly: without that guard a
        partially constructed wrapper would recurse instead of raising ``AttributeError``.
        """
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)
