"""ADR and repository-documentation ingestion (spec sections 29 and 71)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scout_domain.models.knowledge import (
    AUTHORITY_RANK,
    KnowledgeAuthority,
    KnowledgeSourceType,
)
from scout_knowledge.ingest import (
    AdrStatus,
    ingest_adr,
    ingest_documentation_pages,
    ingest_repository_documentation,
    parse_adr_status,
    parse_document_title,
    stable_document_id,
)

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)

_ACCEPTED_ADR = """# 0007 Use PostgreSQL for engineering memory

- Status: Accepted
- Date: 2025-05-01

## Context

We need durable, queryable engineering memory without adding a vector database.

## Decision

Store knowledge documents in PostgreSQL and rank them by authority.
"""

_PROPOSED_ADR = """# 0008 Adopt a message broker

Status: Proposed

We might need asynchronous fan-out later.
"""


def test_parse_adr_status_reads_the_status_line() -> None:
    assert parse_adr_status(_ACCEPTED_ADR) is AdrStatus.ACCEPTED
    assert parse_adr_status(_PROPOSED_ADR) is AdrStatus.PROPOSED


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("# ADR\n\nStatus: Accepted\n", AdrStatus.ACCEPTED),
        ("# ADR\n\n- Status: Proposed\n", AdrStatus.PROPOSED),
        ("# ADR\n\n**Status:** Superseded\n", AdrStatus.SUPERSEDED),
        ("# ADR\n\n## Status\n\nDeprecated\n", AdrStatus.DEPRECATED),
        ("# ADR\n\nstatus: accepted\n", AdrStatus.ACCEPTED),
        ("# ADR\n\nNo status here.\n", AdrStatus.UNKNOWN),
        ("# ADR\n\nStatus: SomethingElse\n", AdrStatus.UNKNOWN),
    ],
)
def test_parse_adr_status_accepts_every_documented_form(content: str, expected: AdrStatus) -> None:
    assert parse_adr_status(content) is expected


def test_parse_document_title_prefers_the_first_heading() -> None:
    assert parse_document_title("docs/adr/0007.md", _ACCEPTED_ADR) == (
        "0007 Use PostgreSQL for engineering memory"
    )


def test_parse_document_title_falls_back_to_the_file_stem() -> None:
    assert parse_document_title("docs/rate-limiting-guide.md", "No heading here.") == (
        "rate limiting guide"
    )


def test_parse_document_title_skips_a_status_heading() -> None:
    content = "## Status\n\nAccepted\n"
    assert parse_document_title("docs/adr/0009-something.md", content) == "0009 something"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (AdrStatus.ACCEPTED, KnowledgeAuthority.APPROVED_DOCUMENT),
        (AdrStatus.PROPOSED, KnowledgeAuthority.REPOSITORY_DOC),
        (AdrStatus.SUPERSEDED, KnowledgeAuthority.REPOSITORY_DOC),
        (AdrStatus.DEPRECATED, KnowledgeAuthority.REPOSITORY_DOC),
        (AdrStatus.UNKNOWN, KnowledgeAuthority.REPOSITORY_DOC),
    ],
)
def test_ingest_adr_assigns_authority_from_status(
    status: AdrStatus, expected: KnowledgeAuthority
) -> None:
    content = f"# 0010 Some decision\n\nStatus: {status.value}\n\nBody text."
    document = ingest_adr("docs/adr/0010.md", content, created_at=NOW)

    assert document.authority is expected
    assert document.source_type is KnowledgeSourceType.ADR
    assert document.source_reference == "docs/adr/0010.md"
    assert document.tags == ["adr", f"adr:{status.value.lower()}"]
    assert document.created_at == NOW
    assert document.updated_at == NOW


def test_accepted_adr_outranks_an_unapproved_one() -> None:
    accepted = ingest_adr("docs/adr/accepted.md", _ACCEPTED_ADR, created_at=NOW)
    proposed = ingest_adr("docs/adr/proposed.md", _PROPOSED_ADR, created_at=NOW)

    assert AUTHORITY_RANK[accepted.authority] > AUTHORITY_RANK[proposed.authority]
    assert AdrStatus.ACCEPTED.is_approved is True
    assert AdrStatus.PROPOSED.is_approved is False


def test_ingest_adr_keeps_the_whole_body_and_parses_the_title() -> None:
    document = ingest_adr("docs/adr/0007.md", _ACCEPTED_ADR, created_at=NOW)

    assert document.title == "0007 Use PostgreSQL for engineering memory"
    assert "## Decision" in document.body
    assert document.body == _ACCEPTED_ADR.strip()


def test_ingest_adr_is_idempotent_for_unchanged_content() -> None:
    first = ingest_adr("docs/adr/0007.md", _ACCEPTED_ADR)
    second = ingest_adr("docs/adr/0007.md", _ACCEPTED_ADR)

    assert first.id == second.id
    assert first.id.startswith("kdoc_")


def test_ingest_adr_rejects_empty_content() -> None:
    with pytest.raises(ValueError, match="is empty"):
        ingest_adr("docs/adr/empty.md", "   \n  ")


def test_ingest_adr_honours_an_explicit_scope() -> None:
    document = ingest_adr("docs/adr/0007.md", _ACCEPTED_ADR, scope="acme/api")

    assert document.scope == "acme/api"


def test_ingest_repository_documentation_scopes_to_the_repository() -> None:
    document = ingest_repository_documentation(
        "docs/testing.md",
        "# Testing strategy\n\nWe test behaviour, not implementation.",
        repository_full_name="acme/api",
        created_at=NOW,
    )

    assert document.title == "Testing strategy"
    assert document.scope == "acme/api"
    assert document.source_type is KnowledgeSourceType.REPOSITORY_DOCUMENTATION
    assert document.authority is KnowledgeAuthority.REPOSITORY_DOC
    assert document.tags == ["documentation"]
    assert document.source_reference == "docs/testing.md"


def test_ingest_repository_documentation_allows_a_reviewed_authority_claim() -> None:
    document = ingest_repository_documentation(
        "docs/architecture.md",
        "# Architecture\n\nReviewed by the platform team.",
        repository_full_name="acme/api",
        authority=KnowledgeAuthority.APPROVED_DOCUMENT,
        tags=["architecture"],
    )

    assert document.authority is KnowledgeAuthority.APPROVED_DOCUMENT
    assert document.tags == ["documentation", "architecture"]


def test_ingest_repository_documentation_requires_a_repository_and_content() -> None:
    with pytest.raises(ValueError, match="repository_full_name is required"):
        ingest_repository_documentation("docs/a.md", "body", repository_full_name="  ")
    with pytest.raises(ValueError, match="is empty"):
        ingest_repository_documentation("docs/a.md", " ", repository_full_name="acme/api")


def test_ingest_documentation_pages_sorts_by_path() -> None:
    documents = ingest_documentation_pages(
        {
            "docs/z.md": "# Zeta",
            "docs/a.md": "# Alpha",
        },
        repository_full_name="acme/api",
        created_at=NOW,
    )

    assert [document.source_reference for document in documents] == ["docs/a.md", "docs/z.md"]
    assert {document.scope for document in documents} == {"acme/api"}


def test_stable_document_id_is_deterministic_and_content_sensitive() -> None:
    assert stable_document_id("adr", "a.md", "one") == stable_document_id("adr", "a.md", "one")
    assert stable_document_id("adr", "a.md", "one") != stable_document_id("adr", "a.md", "two")
    assert stable_document_id("adr", "a.md", "one").startswith("kdoc_")
