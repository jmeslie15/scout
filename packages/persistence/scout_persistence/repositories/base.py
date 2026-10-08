"""Shared repository plumbing.

Every repository takes the same :class:`~sqlalchemy.ext.asyncio.AsyncSession` in its
constructor and flushes at most — it never commits. The commit boundary belongs to
:class:`scout_persistence.unit_of_work.UnitOfWork`, so a repository method can be
composed into a larger transaction without the caller having to know which of the
things it called happened to commit first.

The helpers here exist because the alternative is a copy-paste that drifts:
:func:`ensure_valid_columns` rejects an unknown keyword before it reaches SQL, so a
typo in an orchestrator's ``**fields`` fails at the call site naming the column rather
than as an ``AttributeError`` from inside SQLAlchemy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.models.base import utcnow

__all__ = ["BaseRepository", "ensure_valid_columns"]


class BaseRepository:
    """Common constructor and column-validation helpers for every repository."""

    __slots__ = ("session",)

    def __init__(self, session: AsyncSession) -> None:
        """Bind this repository to ``session``.

        The session is shared, not cloned: two repositories in one unit of work must
        see each other's uncommitted rows, which is what makes a single transaction
        across aggregates possible.
        """
        self.session = session

    def stamp(self, row: Any, *, now: datetime | None = None) -> None:
        """Set ``row.updated_at`` to ``now`` (defaulting to the current UTC time).

        Stamping in Python rather than relying on SQLAlchemy's ``onupdate`` matters for
        two reasons: ``onupdate`` does not fire for a bulk ``UPDATE ... WHERE``, and a
        test that needs a deterministic clock has to be able to supply one. An
        ``updated_at`` that failed to move on a row whose status did move is a lie in
        the audit trail.
        """
        row.updated_at = now or utcnow()


def ensure_valid_columns(row_type: type[Any], fields: dict[str, Any]) -> None:
    """Raise ``ValueError`` if ``fields`` names anything that is not a mapped column.

    A caller building ``**fields`` from a request body can otherwise pass a key no
    column matches, and SQLAlchemy will either silently ignore it or fail with a
    message that does not name the caller's mistake.
    """
    allowed = {column.key for column in row_type.__mapper__.columns}
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ValueError(
            f"{row_type.__name__} has no column(s) {', '.join(unknown)}; "
            f"permitted columns are {', '.join(sorted(allowed))}"
        )
