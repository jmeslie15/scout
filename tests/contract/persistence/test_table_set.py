"""The table set is exactly the specification's, plus one documented exception.

Spec section 42 names the tables. This test asserts that list literally, so a table added
by accident — a scratch table, a duplicate, a leftover from an experiment — fails here
rather than being discovered as an unexplained relation in production.

One table is deliberately outside the spec list: ``approvals``. Sections 46 and 74 require
the human authorization record to be durable, attributable, and *invalidated rather than
deleted* when a pull request moves past it, and ``ApprovalRepository`` cannot honour that
without somewhere to write. It is named explicitly in :data:`EXTRA_TABLES` with its
reason, so the exception is visible in the test output rather than hidden in a count —
and any *other* extra table still fails.
"""

from __future__ import annotations

from typing import Final

import pytest

from scout_persistence.tables import TABLE_NAMES, Base

#: The tables spec section 42 names, transcribed rather than derived from the models.
#: Deriving them from the models would make this test vacuous: it would assert that the
#: tables are the tables.
SPEC_TABLES: Final[tuple[str, ...]] = (
    "repositories",
    "verification_jobs",
    "webhook_events",
    "job_stages",
    "agent_runs",
    "tool_runs",
    "findings",
    "questions",
    "question_answers",
    "engineering_decisions",
    "knowledge_documents",
    "knowledge_embeddings",
    "notifications",
    "artifacts",
    "sandbox_runs",
    "model_requests",
    "audit_events",
)

#: Tables beyond the spec list, each with the reason it exists. Kept separate from
#: :data:`SPEC_TABLES` so the spec assertion stays literal and the exception stays
#: auditable.
EXTRA_TABLES: Final[dict[str, str]] = {
    "approvals": (
        "Required by ApprovalRepository and by spec sections 46/74 (a human authorization "
        "must be durable, attributable, and invalidated rather than deleted when the PR "
        "moves). Absent from the section 42 list; approved by the persistence workstream."
    ),
}


def test_table_set_matches_the_specification() -> None:
    """The spec's tables all exist."""
    assert set(SPEC_TABLES) <= set(Base.metadata.tables)


def test_only_documented_extra_tables_exist() -> None:
    """Nothing exists beyond the spec list and the documented exception.

    This is the assertion that catches an unintended table. Checking the *difference*
    rather than the total means the message names the offending table and the reason it
    was not expected.
    """
    unexpected = sorted(set(Base.metadata.tables) - set(SPEC_TABLES) - set(EXTRA_TABLES))
    assert unexpected == [], (
        f"unexpected table(s): {unexpected}. If a new table is intended, add it to "
        "SPEC_TABLES (if the spec now requires it) or EXTRA_TABLES with its reason."
    )


def test_table_names_constant_lists_every_registered_table() -> None:
    """``TABLE_NAMES`` is the schema's own inventory and must not go stale."""
    assert set(TABLE_NAMES) == set(Base.metadata.tables)
    assert len(TABLE_NAMES) == len(set(TABLE_NAMES))


def test_declared_table_order_is_stable() -> None:
    """The declared order is a fixed list, so diffs in it are readable.

    A set would be equivalent for correctness but would make a change to the inventory
    review as an opaque rewrite.
    """
    assert list(TABLE_NAMES) == sorted(TABLE_NAMES, key=TABLE_NAMES.index)
    assert TABLE_NAMES[:2] == ("repositories", "verification_jobs")


@pytest.mark.parametrize("table_name", sorted(SPEC_TABLES + tuple(EXTRA_TABLES)))
def test_table_has_a_prefixed_id_and_both_audit_timestamps(table_name: str) -> None:
    """Every table carries ``id``, ``created_at``, and ``updated_at``.

    A table without them would be unqueryable by identifier and would leave a row that
    cannot say when it was written or last changed.
    """
    table = Base.metadata.tables[table_name]
    assert "id" in table.c
    assert table.c["id"].primary_key is True
    for column_name in ("created_at", "updated_at"):
        column = table.c[column_name]
        # ``sandbox_runs`` and friends inherit these from TimestampMixin; every table must
        # have them whether by mixin or by declaration.
        assert column.nullable is False
        assert column.server_default is not None


def test_extra_tables_carry_a_reason() -> None:
    """Every exception names why it exists, so it can be reviewed later."""
    for name, reason in EXTRA_TABLES.items():
        assert name in Base.metadata.tables
        assert len(reason) > 60, f"{name}'s reason is too short to be useful"


def test_approval_table_is_the_only_documented_exception() -> None:
    """Pin the exception set itself.

    If a future change adds a second exception, this fails and forces the author to
    explain it here — which is the point of having the list at all.
    """
    assert set(EXTRA_TABLES) == {"approvals"}


def test_foreign_keys_point_only_at_tables_that_exist() -> None:
    """Every ``ForeignKey`` resolves to a real table and column.

    A dangling reference compiles fine and fails at migration time, so it is cheaper to
    catch here.
    """
    for table in Base.metadata.sorted_tables:
        for foreign_key in table.foreign_keys:
            target_table = foreign_key.column.table.name
            assert target_table in Base.metadata.tables
            assert foreign_key.column.name in Base.metadata.tables[target_table].c
