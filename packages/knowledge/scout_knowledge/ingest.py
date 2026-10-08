"""Ingestion of repository documentation into engineering memory (spec sections 29, 71).

Ingestion is a set of pure functions over ``(path, content)`` so it is trivially
testable and so the same code can run in the API process (a webhook-driven re-index) and
in the worker without a database. The only side effect is the id: it is derived from a
digest of the path and content, which makes re-ingesting an unchanged document an
idempotent upsert instead of a duplicate row.

Authority is assigned from evidence in the document itself, never from path convention
alone:

* an ADR whose status is ``Accepted`` is an ``APPROVED_DOCUMENT`` — a human approved it;
* ``Proposed``, ``Superseded``, and ``Deprecated`` ADRs, and ADRs with no parseable
  status, are ``REPOSITORY_DOC`` and carry less weight;
* repository documentation is ``REPOSITORY_DOC`` unless a caller asserts otherwise.

Fail-safe direction: an unapproved or unparseable document is *never* promoted to a
higher authority. A policy that over-ranks a proposal is worse than one that under-ranks
an approval, because the failure is invisible.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Final

from scout_domain.ids import CROCKFORD_ALPHABET, IdKind
from scout_domain.models.base import utcnow
from scout_domain.models.knowledge import (
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)

__all__ = [
    "ADR_AUTHORITY",
    "AdrStatus",
    "ingest_adr",
    "ingest_documentation_pages",
    "ingest_repository_documentation",
    "parse_adr_status",
    "parse_document_title",
    "stable_document_id",
]

#: Length of the ULID-shaped suffix used in deterministic document ids.
_ID_SUFFIX_LENGTH: Final[int] = 26
_BASE32: Final[int] = 32

_HEADING_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$",
    re.MULTILINE,
)

#: Matches ``Status: Accepted``, ``- Status: Accepted``, ``**Status:** Accepted``, and a
#: ``## Status`` heading followed by the value on the next line.
_STATUS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?(?:[-*]\s*)?\**\s*status\**\s*:?\s*\**\s*(?P<status>[A-Za-z]+)",
    re.IGNORECASE | re.MULTILINE,
)


class AdrStatus(StrEnum):
    """Lifecycle status of an architecture decision record."""

    ACCEPTED = "Accepted"
    PROPOSED = "Proposed"
    SUPERSEDED = "Superseded"
    DEPRECATED = "Deprecated"
    UNKNOWN = "Unknown"

    @property
    def is_approved(self) -> bool:
        """Whether the status represents a human-approved decision."""
        return self is AdrStatus.ACCEPTED


#: Authority assigned per ADR status. Only ``Accepted`` reaches ``APPROVED_DOCUMENT``.
ADR_AUTHORITY: Final[dict[AdrStatus, KnowledgeAuthority]] = {
    AdrStatus.ACCEPTED: KnowledgeAuthority.APPROVED_DOCUMENT,
    AdrStatus.PROPOSED: KnowledgeAuthority.REPOSITORY_DOC,
    AdrStatus.SUPERSEDED: KnowledgeAuthority.REPOSITORY_DOC,
    AdrStatus.DEPRECATED: KnowledgeAuthority.REPOSITORY_DOC,
    AdrStatus.UNKNOWN: KnowledgeAuthority.REPOSITORY_DOC,
}


def stable_document_id(*parts: str) -> str:
    """Derive a deterministic ``kdoc_`` id from ``parts``.

    A content-addressed id means a re-ingest updates the existing row rather than adding
    a second copy of the same ADR, which is what makes ``upsert`` a safe operation for a
    callback that may fire more than once.
    """
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).digest()
    value = int.from_bytes(digest, "big")
    characters: list[str] = []
    for _ in range(_ID_SUFFIX_LENGTH):
        value, remainder = divmod(value, _BASE32)
        characters.append(CROCKFORD_ALPHABET[remainder])
    suffix = "".join(reversed(characters))
    return f"{IdKind.KNOWLEDGE_DOCUMENT.value}_{suffix}"


def parse_adr_status(content: str) -> AdrStatus:
    """Return the ADR status declared in ``content``, or ``AdrStatus.UNKNOWN``."""
    match = _STATUS_PATTERN.search(content)
    if match is None:
        return AdrStatus.UNKNOWN
    raw = match.group("status").strip().lower()
    for status in AdrStatus:
        if status.value.lower() == raw:
            return status
    return AdrStatus.UNKNOWN


def parse_document_title(path: str, content: str) -> str:
    """Return the document's first heading, falling back to a tidied file stem."""
    for match in _HEADING_PATTERN.finditer(content):
        title = match.group("title").strip().strip("#").strip()
        # A ``## Status`` heading is metadata, not the document title.
        if title and title.lower() != "status":
            return title
    stem = PurePosixPath(path).stem
    return stem.replace("-", " ").replace("_", " ").strip() or path


def ingest_adr(
    path: str,
    content: str,
    *,
    scope: str = "*",
    created_at: datetime | None = None,
) -> KnowledgeDocument:
    """Parse one ADR into a knowledge document.

    ``scope`` defaults to ``*`` (organisation-wide) because an ADR is normally a
    repository-level decision; callers indexing a specific repository should pass its
    ``owner/name`` so retrieval can prefer it over unrelated repositories.
    """
    if not content.strip():
        raise ValueError(f"ADR at {path!r} is empty")

    status = parse_adr_status(content)
    moment = created_at or utcnow()
    return KnowledgeDocument(
        id=stable_document_id("adr", path, content),
        title=parse_document_title(path, content)[:300],
        body=content.strip(),
        source_type=KnowledgeSourceType.ADR,
        authority=ADR_AUTHORITY[status],
        scope=scope,
        tags=["adr", f"adr:{status.value.lower()}"],
        source_reference=path,
        created_at=moment,
        updated_at=moment,
    )


def ingest_repository_documentation(
    path: str,
    content: str,
    *,
    repository_full_name: str,
    scope: str | None = None,
    authority: KnowledgeAuthority = KnowledgeAuthority.REPOSITORY_DOC,
    source_type: KnowledgeSourceType = KnowledgeSourceType.REPOSITORY_DOCUMENTATION,
    tags: Sequence[str] = (),
    created_at: datetime | None = None,
) -> KnowledgeDocument:
    """Ingest one repository documentation file.

    Documentation is treated as authoritative for *intent* and potentially stale, so the
    default authority is ``REPOSITORY_DOC``. Passing a higher ``authority`` is an explicit
    claim by the caller that a human reviewed the document.
    """
    if not content.strip():
        raise ValueError(f"documentation at {path!r} is empty")
    if not repository_full_name.strip():
        raise ValueError("repository_full_name is required to scope ingested documentation")

    moment = created_at or utcnow()
    document_tags = list(dict.fromkeys(["documentation", *tags]))
    return KnowledgeDocument(
        id=stable_document_id("doc", repository_full_name, path, content),
        title=parse_document_title(path, content)[:300],
        body=content.strip(),
        source_type=source_type,
        authority=authority,
        scope=scope or repository_full_name,
        tags=document_tags,
        source_reference=path,
        created_at=moment,
        updated_at=moment,
    )


def ingest_documentation_pages(
    pages: Mapping[str, str],
    *,
    repository_full_name: str,
    scope: str | None = None,
    tags: Sequence[str] = (),
    created_at: datetime | None = None,
) -> list[KnowledgeDocument]:
    """Ingest a ``path -> content`` mapping, for example a checkout's ``docs/`` tree."""
    return [
        ingest_repository_documentation(
            path,
            content,
            repository_full_name=repository_full_name,
            scope=scope,
            tags=tags,
            created_at=created_at,
        )
        for path, content in sorted(pages.items())
    ]
