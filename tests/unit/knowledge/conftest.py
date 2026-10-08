"""Shared fixtures for engineering-memory tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from scout_domain.models.knowledge import (
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)

#: A fixed clock so recency assertions are deterministic rather than wall-clock dependent.
NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)

DocumentFactory = Callable[..., KnowledgeDocument]


@pytest.fixture
def now() -> datetime:
    """The fixed "current time" used by ranking and store tests."""
    return NOW


@pytest.fixture
def make_document() -> DocumentFactory:
    """Return a factory for valid :class:`KnowledgeDocument` values."""

    def _make(
        *,
        id: str = "kdoc_test",  # noqa: A002 - matches the domain field name
        title: str = "Test document",
        body: str = "Test body",
        authority: KnowledgeAuthority = KnowledgeAuthority.REPOSITORY_DOC,
        source_type: KnowledgeSourceType = KnowledgeSourceType.REPOSITORY_DOCUMENTATION,
        scope: str = "acme/api",
        tags: list[str] | None = None,
        created_at: datetime | None = None,
        updated_at: datetime | None = None,
        expires_at: datetime | None = None,
        superseded_by_id: str | None = None,
    ) -> KnowledgeDocument:
        if created_at is not None:
            moment = created_at
        elif expires_at is not None and expires_at <= NOW:
            # The domain model forbids a document that expires before it was created, so a
            # document expiring before "now" must have been created before that.
            moment = expires_at - timedelta(days=1)
        else:
            moment = NOW
        return KnowledgeDocument(
            id=id,
            title=title,
            body=body,
            source_type=source_type,
            authority=authority,
            scope=scope,
            tags=tags or [],
            created_at=moment,
            updated_at=updated_at or moment,
            expires_at=expires_at,
            superseded_by_id=superseded_by_id,
        )

    return _make
