"""Artifact locator persistence (Rule 15, spec section 21).

This repository only ever writes *locators*. The bytes go to the artifact store, and the
division is deliberate: PostgreSQL holds what can be queried, joined, and retained by
policy, while the store holds what is large and immutable. Nothing in this module
accepts artifact content, and no method here can be extended to — the row has no column
for it.

Content addressing drives the read paths: ``get_by_sha256`` answers "have we already
stored these bytes?", which is what makes storing identical output twice free, and
``list_expired`` supports the retention sweep.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.models.artifact import Artifact, ArtifactKind
from scout_persistence.mapping import from_artifact, to_artifact
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import ArtifactRow

__all__ = ["ArtifactRepository"]


class ArtifactRepository(BaseRepository):
    """Reads and writes ``artifacts`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def record(self, artifact: Artifact) -> ArtifactRow:
        """Insert an artifact locator and return its row."""
        row = from_artifact(artifact)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, artifact_id: str) -> ArtifactRow | None:
        """Return the artifact row with ``artifact_id``, or ``None``."""
        return await self.session.get(ArtifactRow, artifact_id)

    async def get_by_sha256(self, sha256: str) -> ArtifactRow | None:
        """Return the most recently recorded row for a content digest, or ``None``.

        Multiple rows can share a digest (the same bytes stored for two jobs), so the
        newest is returned; the digest, not the id, is what identifies the content.
        """
        return await self.session.scalar(
            select(ArtifactRow)
            .where(ArtifactRow.sha256 == sha256)
            .order_by(ArtifactRow.created_at.desc(), ArtifactRow.id.desc())
            .limit(1)
        )

    async def list_for_job(
        self,
        job_id: str,
        *,
        kind: ArtifactKind | None = None,
    ) -> list[ArtifactRow]:
        """Return artifacts for ``job_id``, newest first, optionally filtered by kind."""
        statement = select(ArtifactRow).where(ArtifactRow.job_id == job_id)
        if kind is not None:
            statement = statement.where(ArtifactRow.kind == kind.value)
        statement = statement.order_by(ArtifactRow.created_at.desc(), ArtifactRow.id.desc())
        result = await self.session.scalars(statement)
        return list(result)

    async def list_for_tool_run(self, tool_run_id: str) -> list[ArtifactRow]:
        """Return the artifacts a tool run produced (stdout, stderr, and report)."""
        result = await self.session.scalars(
            select(ArtifactRow)
            .where(ArtifactRow.tool_run_id == tool_run_id)
            .order_by(ArtifactRow.created_at.asc())
        )
        return list(result)

    async def link_tool_run(self, artifact_id: str, tool_run_id: str) -> ArtifactRow:
        """Attach an artifact to the tool run that produced it.

        Separate from :meth:`record` because the artifact is usually written *before* the
        tool run row exists (output is streamed to the store as it arrives), and the link
        is filled in once the run finishes. That ordering is what lets a crashed tool's
        partial output still be retained and attributed.
        """
        row = await self.session.get(ArtifactRow, artifact_id)
        if row is None:
            raise LookupError(f"no artifact {artifact_id!r}")
        row.tool_run_id = tool_run_id
        self.stamp(row)
        await self.session.flush()
        return row

    async def list_expired(self, now: datetime, *, limit: int = 500) -> list[ArtifactRow]:
        """Return artifacts whose retention window has closed, oldest expiry first."""
        result = await self.session.scalars(
            select(ArtifactRow)
            .where(ArtifactRow.expires_at.is_not(None), ArtifactRow.expires_at <= now)
            .order_by(ArtifactRow.expires_at.asc())
            .limit(limit)
        )
        return list(result)

    @staticmethod
    def to_domain(row: ArtifactRow) -> Artifact:
        """Convert an artifact row into its domain model."""
        return to_artifact(row)
