"""Repository registration and activation (spec section 13).

Repositories are the one place where "upsert" is the right operation rather than an
awkward compromise. GitHub sends ``installation_repositories`` events and repository
renames, and Scout has to converge on what GitHub says the current state is; modelling
that as insert-or-fail would make every sync a caller-side read-modify-write.

The identifier is minted once and preserved across renames. ``full_name`` is unique but
mutable, so the row id — not the name — is what other tables should reference if they
ever gain a foreign key.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.ids import IdKind, new_id
from scout_persistence.mapping import to_repository_ref
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import RepositoryRow

__all__ = ["RepositoryRepository"]


class RepositoryRepository(BaseRepository):
    """Reads and writes ``repositories`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def upsert_by_full_name(
        self,
        full_name: str,
        *,
        github_id: int | None = None,
        default_branch: str | None = None,
        installation_id: int | None = None,
        is_active: bool = True,
    ) -> RepositoryRow:
        """Insert or update the repository identified by ``full_name``.

        A rename arrives as a new ``full_name``; when ``github_id`` is supplied and a row
        with that id already exists, *that* row is updated instead of a second one being
        created. Otherwise a repository rename would silently split its history across two
        identities, and the old row would keep being treated as an active repo.

        ``None`` means "leave unchanged" for the optional fields, while ``is_active`` is
        explicit because deactivation is always a deliberate act.
        """
        row: RepositoryRow | None = None
        if github_id is not None:
            row = await self.session.scalar(
                select(RepositoryRow).where(RepositoryRow.github_id == github_id)
            )
        if row is None:
            row = await self.get_by_full_name(full_name)

        if row is None:
            row = RepositoryRow(
                id=new_id(IdKind.REPOSITORY),
                full_name=full_name,
                github_id=github_id,
                default_branch=default_branch or "main",
                is_active=is_active,
                installation_id=installation_id,
            )
            self.session.add(row)
        else:
            row.full_name = full_name
            if github_id is not None:
                row.github_id = github_id
            if default_branch is not None:
                row.default_branch = default_branch
            if installation_id is not None:
                row.installation_id = installation_id
            row.is_active = is_active
            self.stamp(row)
        await self.session.flush()
        return row

    async def get_by_full_name(self, full_name: str) -> RepositoryRow | None:
        """Return the repository row for ``owner/name``, or ``None``."""
        return await self.session.scalar(
            select(RepositoryRow).where(RepositoryRow.full_name == full_name)
        )

    async def get(self, repository_id: str) -> RepositoryRow | None:
        """Return the repository row with ``repository_id``, or ``None``."""
        return await self.session.get(RepositoryRow, repository_id)

    async def get_by_github_id(self, github_id: int) -> RepositoryRow | None:
        """Return the repository row for a stable GitHub numeric id, or ``None``.

        Preferred over :meth:`get_by_full_name` whenever the numeric id is known: it is
        the identity that survives a rename.
        """
        return await self.session.scalar(
            select(RepositoryRow).where(RepositoryRow.github_id == github_id)
        )

    async def set_active(self, full_name: str, is_active: bool) -> RepositoryRow:
        """Activate or deactivate a repository.

        Deactivation is a flag rather than a delete: the jobs, findings, and audit events
        that reference this repository must remain readable, and a repository that
        temporarily loses its GitHub App installation usually comes back.
        """
        row = await self.get_by_full_name(full_name)
        if row is None:
            raise LookupError(f"no repository {full_name!r}")
        row.is_active = is_active
        self.stamp(row)
        await self.session.flush()
        return row

    async def set_installation_id(
        self,
        full_name: str,
        installation_id: int | None,
    ) -> RepositoryRow:
        """Bind a GitHub App installation id to a repository.

        Stored on the control-plane row only. It is a credential handle and must never
        reach agent context (spec sections 54 and 55).
        """
        row = await self.get_by_full_name(full_name)
        if row is None:
            raise LookupError(f"no repository {full_name!r}")
        row.installation_id = installation_id
        self.stamp(row)
        await self.session.flush()
        return row

    async def list_active(self, *, limit: int = 500) -> list[RepositoryRow]:
        """Return active repositories, alphabetical by name."""
        result = await self.session.scalars(
            select(RepositoryRow)
            .where(RepositoryRow.is_active.is_(True))
            .order_by(RepositoryRow.full_name.asc())
            .limit(limit)
        )
        return list(result)

    async def list_all(self, *, limit: int = 500) -> list[RepositoryRow]:
        """Return every registered repository, including inactive ones."""
        result = await self.session.scalars(
            select(RepositoryRow).order_by(RepositoryRow.full_name.asc()).limit(limit)
        )
        return list(result)

    @staticmethod
    def to_domain_str(row: RepositoryRow) -> str:
        """Return the canonical ``owner/name`` for a row.

        Not a domain conversion: :class:`RepositoryRef` is embedded in a job as JSON, so
        this exists for the many call sites that only need the name.
        """
        return str(row.full_name)

    @staticmethod
    def as_ref(row: RepositoryRow) -> object:
        """Return the :class:`~scout_domain.models.repository.RepositoryRef` for a row."""
        return to_repository_ref(row)
