"""Decision recording, chaining, and the acknowledge-before-flagging hook."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scout_domain.enums import Severity
from scout_domain.errors import ConfigurationError
from scout_domain.models.knowledge import (
    EngineeringDecision,
    KnowledgeAuthority,
    KnowledgeSourceType,
)
from scout_domain.models.question import Question, QuestionAnswer
from scout_knowledge.decisions import DecisionRegistry
from scout_knowledge.models import RetrievalQuery
from scout_knowledge.store import InMemoryKnowledgeStore

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


def _decision(
    *,
    id: str,  # noqa: A002 - matches the domain field name
    statement: str = "Reporting service authorization is enforced at the gateway.",
    rationale: str = "Gateway terminates auth and forwards a signed identity header.",
    scope: str = "acme/api",
    tags: list[str] | None = None,
    decided_at: datetime | None = None,
) -> EngineeringDecision:
    return EngineeringDecision(
        id=id,
        statement=statement,
        rationale=rationale,
        decided_by="dana",
        decided_at=decided_at or NOW,
        scope=scope,
        tags=tags or [],
    )


def _question() -> Question:
    return Question(
        id="qst_1",
        job_id="job_1",
        title="Should managers see export metadata?",
        description="The export endpoint returns metadata for every user.",
        context="Scout could not determine the intended authorization model.",
        investigation_summary="Checked route decorators, policy files, and existing tests.",
        severity=Severity.MEDIUM,
        options=["Managers may see metadata", "Only owners may"],
        created_by="code_reviewer",
    )


def _answer(*, promote: bool = True, scope: str | None = "acme/api") -> QuestionAnswer:
    return QuestionAnswer(
        id="ans_1",
        question_id="qst_1",
        body="Managers may see export metadata; the report is internal-only.",
        answered_by="dana",
        answered_at=NOW,
        promote_to_knowledge=promote,
        knowledge_scope=scope,
    )


async def test_record_without_promotion_keeps_decision_out_of_retrieval() -> None:
    registry = DecisionRegistry(store=InMemoryKnowledgeStore())

    document = await registry.record(_decision(id="dec_plain"), promote_to_knowledge=False)

    assert document is None
    assert [decision.id for decision in registry.decisions] == ["dec_plain"]


async def test_promotion_mints_human_decision_document_and_links_both_ways() -> None:
    store = InMemoryKnowledgeStore()
    registry = DecisionRegistry(store=store)
    decision = _decision(id="dec_promoted", tags=["authorization.gateway"])

    document = await registry.record(decision, promote_to_knowledge=True)

    assert document is not None
    assert document.authority is KnowledgeAuthority.HUMAN_DECISION
    assert document.source_type is KnowledgeSourceType.HUMAN_ANSWER
    assert document.source_reference == decision.id
    assert document.scope == "acme/api"
    assert document.tags == ["authorization.gateway"]

    stored = await store.get(document.id)
    assert stored is not None
    assert stored.id == document.id

    recorded = registry.decisions[0]
    assert recorded.knowledge_document_ids == [document.id]


async def test_promotion_without_a_store_is_a_configuration_error() -> None:
    registry = DecisionRegistry()

    with pytest.raises(ConfigurationError, match="requires a KnowledgeStore"):
        await registry.record(_decision(id="dec_no_store"), promote_to_knowledge=True)


async def test_promoted_document_title_uses_the_first_statement_line() -> None:
    registry = DecisionRegistry(store=InMemoryKnowledgeStore())
    decision = _decision(
        id="dec_multiline",
        statement="Use expand-contract migrations.\nNever drop a column in the same release.",
    )

    document = await registry.record(decision, promote_to_knowledge=True)

    assert document is not None
    assert document.title == "Use expand-contract migrations."
    assert "Never drop a column" in document.body


async def test_find_applicable_filters_by_scope_and_tags() -> None:
    registry = DecisionRegistry()
    await registry.record(
        _decision(id="dec_api", scope="acme/api", tags=["database.migration"]),
        promote_to_knowledge=False,
    )
    await registry.record(
        _decision(id="dec_org", scope="*", tags=["security.secrets"]),
        promote_to_knowledge=False,
    )
    await registry.record(
        _decision(id="dec_other", scope="other/service", tags=["database.migration"]),
        promote_to_knowledge=False,
    )

    in_scope = registry.find_applicable("acme/api")
    tagged = registry.find_applicable("acme/api", tags=["database.migration"])

    assert {decision.id for decision in in_scope} == {"dec_api", "dec_org"}
    assert [decision.id for decision in tagged] == ["dec_api"]


async def test_find_applicable_ignores_superseded_and_supports_time_travel() -> None:
    registry = DecisionRegistry()
    first = _decision(id="dec_first", tags=["database.migration"], decided_at=NOW)
    await registry.record(first, promote_to_knowledge=False)

    reversal = _decision(
        id="dec_second",
        statement="Reversed: migrations now run through the platform pipeline.",
        tags=["database.migration"],
        decided_at=NOW + timedelta(days=1),
    )
    await registry.supersede("dec_first", reversal)

    assert [decision.id for decision in registry.find_applicable("acme/api")] == ["dec_second"]
    assert [decision.id for decision in registry.find_applicable("acme/api", as_of=NOW)] == [
        "dec_first"
    ]


async def test_supersede_chains_both_directions() -> None:
    registry = DecisionRegistry()
    original = _decision(id="dec_original")
    await registry.record(original, promote_to_knowledge=False)

    replacement = await registry.supersede("dec_original", _decision(id="dec_replacement"))

    assert replacement.supersedes_id == "dec_original"
    by_id = {decision.id: decision for decision in registry.decisions}
    assert by_id["dec_original"].superseded_by_id == "dec_replacement"
    assert by_id["dec_original"].is_active is False
    assert by_id["dec_replacement"].is_active is True


async def test_supersede_rejects_an_unknown_decision() -> None:
    registry = DecisionRegistry()

    with pytest.raises(KeyError, match="unknown engineering decision"):
        await registry.supersede("dec_missing", _decision(id="dec_new"))


async def test_supersede_with_promotion_retires_the_old_knowledge_document() -> None:
    store = InMemoryKnowledgeStore()
    registry = DecisionRegistry(store=store)
    original = _decision(id="dec_old", statement="Use MySQL for the audit log.")
    old_document = await registry.record(original, promote_to_knowledge=True)
    assert old_document is not None

    replacement = await registry.supersede(
        "dec_old",
        _decision(id="dec_new", statement="Use PostgreSQL for the audit log."),
        promote_to_knowledge=True,
    )
    assert replacement.supersedes_id == "dec_old"
    assert len(replacement.knowledge_document_ids) == 1
    new_document_id = replacement.knowledge_document_ids[0]
    assert await store.get(new_document_id) is not None

    retired = await store.get(old_document.id)
    assert retired is not None
    assert retired.superseded_by_id == new_document_id


def test_from_question_answer_captures_provenance() -> None:
    question = _question()
    answer = _answer()

    decision = DecisionRegistry.from_question_answer(
        answer,
        question,
        knowledge_scope=answer.knowledge_scope or "*",
    )

    assert decision.statement == answer.body
    assert decision.decided_by == answer.answered_by
    assert decision.decided_at == answer.answered_at
    assert decision.scope == "acme/api"
    assert decision.source_question_id == question.id
    assert decision.source_job_id == question.job_id
    assert decision.source_answer_id == answer.id
    assert question.investigation_summary in decision.rationale


async def test_promoted_answer_becomes_retrievable_knowledge() -> None:
    store = InMemoryKnowledgeStore()
    registry = DecisionRegistry(store=store)
    question = _question()
    answer = _answer()
    question = question.with_answer(answer)

    decision = DecisionRegistry.from_question_answer(
        answer,
        question,
        knowledge_scope=answer.knowledge_scope or "*",
    )
    await registry.record(decision, promote_to_knowledge=answer.promote_to_knowledge)

    result = await store.search(
        RetrievalQuery(text="managers export metadata", repository_full_name="acme/api")
    )
    assert [item.document.authority for item in result.items] == [KnowledgeAuthority.HUMAN_DECISION]


async def test_acknowledge_before_flagging_returns_the_matching_decision() -> None:
    registry = DecisionRegistry()
    await registry.record(
        _decision(id="dec_gateway", tags=["security.missing_authorization"]),
        promote_to_knowledge=False,
    )

    acknowledged = registry.acknowledge_before_flagging(
        "security.missing_authorization",
        "acme/api",
        registry.find_applicable("acme/api"),
    )

    assert acknowledged is not None
    assert acknowledged.id == "dec_gateway"


async def test_acknowledge_before_flagging_returns_none_for_an_unrelated_issue() -> None:
    registry = DecisionRegistry()
    await registry.record(
        _decision(id="dec_gateway", tags=["security.missing_authorization"]),
        promote_to_knowledge=False,
    )

    assert (
        registry.acknowledge_before_flagging(
            "tests.missing_coverage",
            "acme/api",
            registry.find_applicable("acme/api"),
        )
        is None
    )


async def test_acknowledge_before_flagging_ignores_superseded_and_out_of_scope() -> None:
    registry = DecisionRegistry()
    await registry.record(
        _decision(id="dec_gateway", tags=["security.missing_authorization"]),
        promote_to_knowledge=False,
    )
    await registry.supersede(
        "dec_gateway",
        _decision(id="dec_revised", statement="Authorization is now enforced in-process."),
    )

    superseded = registry.acknowledge_before_flagging(
        "security.missing_authorization",
        "acme/api",
        registry.decisions,
    )
    wrong_repo = registry.acknowledge_before_flagging(
        "security.missing_authorization",
        "other/service",
        registry.decisions,
    )

    assert superseded is None
    assert wrong_repo is None


async def test_acknowledge_before_flagging_matches_statement_terms() -> None:
    registry = DecisionRegistry()
    await registry.record(
        _decision(
            id="dec_terms",
            statement="The reporting service bypasses user authorization at the route layer.",
            rationale="Access is enforced at the gateway instead.",
        ),
        promote_to_knowledge=False,
    )

    acknowledged = registry.acknowledge_before_flagging(
        "reporting authorization bypass",
        "acme/api",
        registry.decisions,
    )

    assert acknowledged is not None
    assert acknowledged.id == "dec_terms"


def test_acknowledge_before_flagging_handles_an_empty_signature() -> None:
    registry = DecisionRegistry()

    assert registry.acknowledge_before_flagging("   ", "acme/api", []) is None


# ---------------------------------------------------------------------------------
# record_from_answer: the answer endpoint's entry point
# ---------------------------------------------------------------------------------


async def test_record_from_answer_mints_a_document_without_a_store() -> None:
    """The API builds a bare registry, mints the document, and persists it itself."""
    registry = DecisionRegistry()

    document = await registry.record_from_answer(
        answer=_answer(promote=True),
        title="Should managers see export metadata?",
        scope="acme/api",
    )

    assert document is not None
    assert document.authority is KnowledgeAuthority.HUMAN_DECISION
    assert document.source_type is KnowledgeSourceType.HUMAN_ANSWER
    assert document.title == "Should managers see export metadata?"
    assert document.scope == "acme/api"
    assert document.source_reference == registry.decisions[0].id
    assert registry.decisions[0].knowledge_document_ids == [document.id]
    assert registry.decisions[0].source_answer_id == "ans_1"
    assert registry.decisions[0].source_question_id == "qst_1"


async def test_record_from_answer_returns_none_when_not_promoting() -> None:
    registry = DecisionRegistry()

    document = await registry.record_from_answer(
        answer=_answer(promote=False),
        title="Should managers see export metadata?",
        scope="acme/api",
    )

    assert document is None
    assert registry.decisions == []


async def test_record_from_answer_persists_when_a_store_is_configured() -> None:
    store = InMemoryKnowledgeStore()
    registry = DecisionRegistry(store=store)

    document = await registry.record_from_answer(
        answer=_answer(promote=True),
        title="Should managers see export metadata?",
        scope="acme/api",
    )

    assert document is not None
    assert await store.get(document.id) is not None
    result = await store.search(
        RetrievalQuery(text="managers export metadata", repository_full_name="acme/api")
    )
    assert document.id in {item.document.id for item in result.items}


async def test_record_from_answer_defaults_an_empty_scope_to_organisation_wide() -> None:
    registry = DecisionRegistry()
    answer = _answer(promote=True, scope=None)

    document = await registry.record_from_answer(
        answer=answer,
        title="Cross-cutting decision",
        scope="   ",
    )

    assert document is not None
    assert document.scope == "*"


async def test_record_from_answer_truncates_an_overlong_title() -> None:
    registry = DecisionRegistry()

    document = await registry.record_from_answer(
        answer=_answer(promote=True),
        title="x" * 400,
        scope="acme/api",
    )

    assert document is not None
    assert len(document.title) == 300


async def test_record_from_answer_falls_back_to_the_statement_for_an_empty_title() -> None:
    registry = DecisionRegistry()

    document = await registry.record_from_answer(
        answer=_answer(promote=True),
        title="   ",
        scope="acme/api",
    )

    assert document is not None
    assert document.title == "Managers may see export metadata; the report is internal-only."
