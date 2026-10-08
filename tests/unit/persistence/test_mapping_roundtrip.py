"""Round-trip proof for every row/model conversion.

The claim being tested is specific: a domain model written by :func:`from_domain` and
read back by :func:`to_domain` is *equal to the original*. Equality, not
"approximately equal" — Pydantic models compare by field, so any dropped field, any
enum written as the wrong spelling, and any timestamp that loses its timezone shows up
as a failure rather than as a silently thinner row.

Every entity with a registered mapping is covered, not just the six the brief names.
A conversion that is only exercised by the entity someone remembered is a conversion
that will break on the entity nobody remembered.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scout_domain.enums import (
    ActorType,
    AgentRole,
    AgentStatus,
    ApprovalDecision,
    Capability,
    EvidenceKind,
    FindingCategory,
    JobStatus,
    NotificationChannel,
    QuestionStatus,
    Recommendation,
    RiskLevel,
    SandboxBackendKind,
    Severity,
    ToolStatus,
    VerificationStatus,
)
from scout_domain.ids import IdKind, new_id
from scout_domain.models.agent import AgentRun
from scout_domain.models.approval import Approval
from scout_domain.models.artifact import Artifact, ArtifactKind
from scout_domain.models.audit import AuditEvent
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.finding import Finding, FindingOrigin
from scout_domain.models.job import CommitRange, JobStage, JobStageOutcome, VerificationJob
from scout_domain.models.knowledge import (
    EngineeringDecision,
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from scout_domain.models.model_request import ModelRequestRecord, ModelRequestStatus
from scout_domain.models.notification import NotificationRecord, NotificationStatus
from scout_domain.models.question import Question, QuestionAnswer
from scout_domain.models.repository import RepositoryRef, TechnologyStack
from scout_domain.models.sandbox import (
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRun,
    SandboxStatus,
)
from scout_domain.models.tool import ToolInvocation, ToolResult, ToolSummary
from scout_persistence.mapping import (
    ENTITY_MAPPINGS,
    from_domain,
    to_domain,
    to_from_domain_row,
    to_question,
)

#: A fixed instant with a non-UTC offset on purpose: a naive column would strip the
#: offset and the equality assertion would fail, which is exactly the regression this
#: file exists to catch.
AWARE: datetime = datetime(2026, 3, 4, 5, 6, 7, 890123, tzinfo=UTC)
LATER: datetime = AWARE + timedelta(minutes=17)


def assert_round_trips[ModelT](model: ModelT) -> None:
    """Assert ``model`` survives a trip through its row and back unchanged."""
    restored = to_from_domain_row(model)
    assert restored == model, f"{type(model).__name__} did not survive a round trip"


def _job(job_id: str = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0") -> VerificationJob:
    return VerificationJob(
        id=job_id,
        repository=RepositoryRef(full_name="acme/widgets", github_id=991, default_branch="trunk"),
        repository_full_name="acme/widgets",
        pull_request_number=42,
        pull_request_title="Add widget export",
        pull_request_author="octocat",
        pull_request_url="https://github.com/acme/widgets/pull/42",
        commits=CommitRange(base_sha="a" * 40, head_sha="b" * 40, merge_base_sha="c" * 40),
        github_delivery_id="delivery-1",
        status=JobStatus.WAITING_FOR_HUMAN,
        risk_level=RiskLevel.HIGH,
        verification_status=VerificationStatus.PASSED_WITH_WARNINGS,
        recommendation=Recommendation.REQUIRES_HUMAN_REVIEW,
        attempt=2,
        max_attempts=3,
        created_at=AWARE,
        started_at=AWARE,
        completed_at=LATER,
        lease_owner="worker-1",
        lease_expires_at=LATER,
        correlation_id="corr-1",
        failure_reason=None,
        cancel_reason=None,
    )


def test_verification_job_round_trips() -> None:
    """``VerificationJob`` — the aggregate everything else hangs off."""
    assert_round_trips(_job())


def test_verification_job_projects_commit_shas_onto_columns() -> None:
    """The row carries ``head_sha``/``base_sha`` as columns, not only inside ``commits``.

    They are the keys of the PR-revision uniqueness constraint and of every "which
    revision did we verify?" query, so losing the projection would silently disable the
    idempotency guarantee.
    """
    job = _job()
    row = from_domain(job)
    assert row.head_sha == job.commits.head_sha
    assert row.base_sha == job.commits.base_sha
    assert row.repository_full_name == job.repository.full_name


def test_verification_job_none_recommendation_survives() -> None:
    """``recommendation`` is nullable and must stay ``None``, not become a default."""
    job = _job().evolve(recommendation=None)
    assert from_domain(job).recommendation is None
    assert_round_trips(job)


def test_finding_round_trips_with_evidence() -> None:
    """``Finding`` — including the evidence references it cites."""
    finding = Finding(
        id=new_id(IdKind.FINDING),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        severity=Severity.CRITICAL,
        category=FindingCategory.SECURITY,
        title="Export bypasses authorization",
        description="The new export endpoint reads another tenant's rows.",
        repository="acme/widgets",
        file="src/export.py",
        line_start=48,
        line_end=61,
        evidence=[
            EvidenceReference(
                kind=EvidenceKind.FILE_RANGE,
                reference_id="src/export.py",
                summary="No authorization check before the query.",
                file_path="src/export.py",
                line_start=48,
                line_end=61,
                excerpt="return session.execute(select(Row))",
            ),
            EvidenceReference(
                kind=EvidenceKind.TOOL_RUN,
                reference_id="trun_01J8ZQ4K2M7X3N9PQRSTVWXY0",
                summary="bandit flagged the raw query.",
                artifact_id="art_01J8ZQ4K2M7X3N9PQRSTVWXY0",
                severity=Severity.HIGH,
            ),
        ],
        recommendation="Require a gateway-level authorization check.",
        confidence=0.95,
        origin=FindingOrigin.AGENT,
        source="security",
        source_run_id="arun_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        created_at=AWARE,
    )
    assert_round_trips(finding)


def test_finding_confidence_capped_flag_survives() -> None:
    """The ``confidence_capped`` flag is persisted, not recomputed.

    It is the only record that a model's claimed confidence was clamped by the evidence
    ceiling. Recomputing it on read would make the flag depend on the evidence drifting
    later, which is not what the finding said when it was produced.
    """
    finding = Finding(
        id=new_id(IdKind.FINDING),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        severity=Severity.LOW,
        category=FindingCategory.MAINTAINABILITY,
        title="Long function",
        description="The function is 300 lines.",
        repository="acme/widgets",
        confidence=0.99,
        origin=FindingOrigin.AGENT,
        source="code_reviewer",
    )
    assert finding.confidence_capped is True, "fixture must exercise the clamped path"
    assert finding.requires_human is True, "unsupported findings must force human review"
    assert_round_trips(finding)


def test_question_round_trips_open() -> None:
    """``Question`` with no answers — the shape the create path writes."""
    question = Question(
        id=new_id(IdKind.QUESTION),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        title="Should managers see export metadata?",
        description="The change makes export metadata manager-visible.",
        context="Scout could not find an authorization decision.",
        investigation_summary="Checked the ADRs and the gateway configuration.",
        severity=Severity.MEDIUM,
        options=["Yes, managers may", "No, admins only"],
        default_assumption="Assume admins only; no metadata leaves the admin surface.",
        status=QuestionStatus.OPEN,
        created_by="security",
        created_by_type=ActorType.AGENT,
        created_at=AWARE,
    )
    assert_round_trips(question)


def test_question_answers_round_trip_through_their_own_table() -> None:
    """``QuestionAnswer`` rows rebuild the question's ``answers`` list.

    Answers live in ``question_answers`` rather than in a JSONB column on the question,
    so the generic :func:`to_from_domain_row` — which has one row and no session —
    cannot reconstruct them. The real read path is
    :meth:`QuestionRepository.get`, which loads the answer rows and passes them in. This
    test exercises that shape directly, so the conversion that matters is covered rather
    than only the one that happens to fit the generic helper.
    """
    answer = QuestionAnswer(
        id=new_id(IdKind.ANSWER),
        question_id="qst_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        body="Yes, managers may see export metadata; the gateway enforces tenant scope.",
        answered_by="dana",
        answered_by_type=ActorType.HUMAN,
        answered_at=LATER,
        promote_to_knowledge=True,
        knowledge_scope="acme/widgets",
    )
    question = Question(
        id="qst_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        title="Should managers see export metadata?",
        description="The change makes export metadata manager-visible.",
        status=QuestionStatus.ANSWERED,
        created_by="security",
        created_at=AWARE,
        answers=[answer],
        accepted_answer_id=answer.id,
    )

    question_row = from_domain(question)
    answer_row = from_domain(answer)
    restored = to_question(question_row, [answer_row])

    assert restored == question
    assert restored.answers == [answer]
    assert restored.accepted_answer == answer


def test_question_answer_round_trips_on_its_own() -> None:
    """The answer row is a first-class conversion, not only a nested one."""
    answer = QuestionAnswer(
        id=new_id(IdKind.ANSWER),
        question_id="qst_1",
        body="Yes, with tenant scoping.",
        answered_by="dana",
        answered_by_type=ActorType.HUMAN,
        answered_at=LATER,
        promote_to_knowledge=True,
        knowledge_scope="acme/widgets",
    )
    assert_round_trips(answer)


def test_tool_result_round_trips() -> None:
    """``ToolResult`` — locators for output, never the output itself."""
    result = ToolResult(
        id=new_id(IdKind.TOOL_RUN),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        stage=JobStatus.TESTING.value,
        tool="pytest",
        version="8.3.4",
        command="pytest -q tests/",
        argv=["pytest", "-q", "tests/"],
        exit_code=1,
        status=ToolStatus.FAILED,
        started_at=AWARE,
        completed_at=LATER,
        duration_seconds=1020.5,
        stdout_artifact_id="art_stdout",
        stderr_artifact_id="art_stderr",
        report_artifact_id="art_report",
        summary=ToolSummary(values={"passed": 180, "failed": 3, "duration_seconds": 1020.5}),
        error=None,
        infrastructure_failure=False,
        sandbox_run_id="srun_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        narrative="3 failures in the export suite.",
        metadata={"attempt": 1, "rerun": False, "suite": "export"},
    )
    assert_round_trips(result)


def test_tool_result_preserves_json_scalar_types() -> None:
    """``metadata`` values keep their JSON types through the round trip.

    A value that came back as ``"1"`` instead of ``1`` would change how a report reads
    it, and the difference is invisible in a rendered template.
    """
    result = ToolResult(
        id=new_id(IdKind.TOOL_RUN),
        job_id="job_1",
        tool="pytest",
        command="pytest",
        status=ToolStatus.PASSED,
        exit_code=0,
        started_at=AWARE,
        completed_at=LATER,
        duration_seconds=1.0,
        metadata={"integer": 1, "float": 1.5, "boolean": True, "text": "x", "null": None},
    )
    restored = to_from_domain_row(result)
    assert restored.metadata == {
        "integer": 1,
        "float": 1.5,
        "boolean": True,
        "text": "x",
        "null": None,
    }


def test_approval_round_trips_valid() -> None:
    """``Approval`` — the human authorization record."""
    approval = Approval(
        id=new_id(IdKind.APPROVAL),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        commit_sha="b" * 40,
        decision=ApprovalDecision.APPROVE,
        decided_by="dana",
        decided_by_type=ActorType.HUMAN,
        decided_at=AWARE,
        comment="Verified the export path by hand.",
    )
    assert_round_trips(approval)


def test_approval_round_trips_invalidated() -> None:
    """Invalidation is a column pair, so both halves must survive."""
    approval = Approval(
        id=new_id(IdKind.APPROVAL),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        commit_sha="b" * 40,
        decision=ApprovalDecision.REQUEST_CHANGES,
        decided_by="dana",
        decided_at=AWARE,
        comment="Needs a gateway check.",
    ).invalidate(reason="PR moved to a new commit", at=LATER)
    assert approval.is_valid is False
    assert_round_trips(approval)


def test_artifact_round_trips() -> None:
    """``Artifact`` — the locator whose bytes live elsewhere."""
    artifact = Artifact(
        id=new_id(IdKind.ARTIFACT),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        kind=ArtifactKind.TOOL_STDOUT,
        content_type="text/plain; charset=utf-8",
        size_bytes=41_943_040,
        sha256="d" * 64,
        storage_key=f"sha256/dd/dd/{'d' * 64}",
        filename="pytest-stdout.txt",
        tool_run_id="trun_1",
        agent_run_id=None,
        sandbox_run_id="srun_1",
        created_at=AWARE,
    )
    assert_round_trips(artifact)


def test_job_stage_round_trips_resolved() -> None:
    """``JobStage`` — an explicit skip, with the reason that makes it auditable."""
    stage = JobStage(
        id=new_id(IdKind.STAGE),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        stage=JobStatus.SECURITY_SCANNING,
        outcome=JobStageOutcome.SKIPPED,
        attempt=1,
        started_at=AWARE,
        completed_at=LATER,
        duration_seconds=0.25,
        skip_reason="Risk is LOW and the change touches documentation only.",
        artifact_ids=["art_1", "art_2"],
        created_at=AWARE,
        updated_at=LATER,
    )
    assert_round_trips(stage)


def test_agent_run_round_trips() -> None:
    """``AgentRun`` — provenance and cost, with no context text stored."""
    run = AgentRun(
        id=new_id(IdKind.AGENT_RUN),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        role=AgentRole.SECURITY,
        status=AgentStatus.NEEDS_HUMAN,
        prompt_version="security@2026-03-01",
        provider="anthropic",
        model="claude-sonnet-4",
        started_at=AWARE,
        completed_at=LATER,
        latency_seconds=17.5,
        input_tokens=12_345,
        output_tokens=678,
        estimated_cost_usd=0.0412,
        context_digest="e" * 64,
        result_artifact_id="art_result",
        error=None,
        created_at=AWARE,
    )
    assert_round_trips(run)


def test_model_request_round_trips() -> None:
    """``ModelRequestRecord`` — accounting, with a prompt digest instead of the prompt."""
    record = ModelRequestRecord(
        id=new_id(IdKind.MODEL_REQUEST),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        agent_run_id="arun_1",
        agent=AgentRole.CODE_REVIEWER,
        provider="openai",
        model="gpt-5",
        prompt_version="code_reviewer@2026-03-01",
        input_tokens=900,
        output_tokens=250,
        latency_seconds=3.25,
        status=ModelRequestStatus.SUCCEEDED,
        estimated_cost_usd=0.0075,
        prompt_digest="f" * 64,
        attempt=2,
        error=None,
        created_at=AWARE,
    )
    assert_round_trips(record)


def test_notification_round_trips_sent() -> None:
    """``NotificationRecord`` — a delivered message with its dedup key."""
    record = NotificationRecord(
        id=new_id(IdKind.NOTIFICATION),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        question_id="qst_1",
        finding_id=None,
        channel=NotificationChannel.GOOGLE_CHAT,
        severity=Severity.HIGH,
        subject="Scout needs a decision",
        body="Should managers see export metadata?",
        dedup_key="question:qst_1:severity",
        status=NotificationStatus.SENT,
        attempts=1,
        sent_at=LATER,
        error=None,
        created_at=AWARE,
    )
    assert_round_trips(record)


def test_knowledge_document_round_trips() -> None:
    """``KnowledgeDocument`` — engineering memory with its authority rank."""
    document = KnowledgeDocument(
        id=new_id(IdKind.KNOWLEDGE_DOCUMENT),
        title="Export metadata authorization",
        body="The reporting service bypasses user authorization; the gateway enforces it.",
        source_type=KnowledgeSourceType.HUMAN_ANSWER,
        authority=KnowledgeAuthority.HUMAN_DECISION,
        scope="acme/widgets",
        tags=["authorization", "export"],
        source_reference="qst_1/ans_1",
        embedding_id=None,
        created_at=AWARE,
        updated_at=LATER,
        superseded_by_id=None,
        expires_at=LATER + timedelta(days=30),
    )
    assert_round_trips(document)


def test_knowledge_document_authority_rank_matches_domain() -> None:
    """The stored ``authority_rank`` is the domain's rank, not a second opinion."""
    from scout_domain.models.knowledge import AUTHORITY_RANK

    for authority in KnowledgeAuthority:
        document = KnowledgeDocument(
            id=new_id(IdKind.KNOWLEDGE_DOCUMENT),
            title="T",
            body="B",
            source_type=KnowledgeSourceType.ARCHITECTURE_DOCUMENT,
            authority=authority,
            created_at=AWARE,
            updated_at=AWARE,
        )
        assert from_domain(document).authority_rank == AUTHORITY_RANK[authority]


def test_engineering_decision_round_trips() -> None:
    """``EngineeringDecision`` — the strongest memory Scout holds."""
    decision = EngineeringDecision(
        id=new_id(IdKind.DECISION),
        statement="The reporting service intentionally bypasses user authorization.",
        rationale="Access is enforced at the gateway.",
        decided_by="dana",
        decided_at=AWARE,
        scope="acme/widgets",
        tags=["authorization"],
        source_question_id="qst_1",
        source_job_id="job_1",
        source_answer_id="ans_1",
        supersedes_id=None,
        superseded_by_id=None,
        knowledge_document_ids=["kdoc_1"],
    )
    assert_round_trips(decision)


def test_sandbox_run_round_trips_with_policy_snapshot() -> None:
    """``SandboxRun`` keeps the limits and policy that were enforced, not the current ones."""
    run = SandboxRun(
        id=new_id(IdKind.SANDBOX_RUN),
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        backend=SandboxBackendKind.DOCKER,
        image="scout-sandbox-python:3.12",
        limits=ResourceLimits(memory_mb=2048, cpus=1.0, pids_limit=256),
        policy=SandboxPolicy(network_mode=NetworkMode.NONE, read_only_rootfs=True),
        status=SandboxStatus.SUCCEEDED,
        workspace_path="/var/workspaces/job_1",
        started_at=AWARE,
        completed_at=LATER,
        duration_seconds=300.5,
        tool_run_ids=["trun_1", "trun_2"],
        error=None,
        native_id="container-abc123",
    )
    assert_round_trips(run)


def test_audit_event_round_trips() -> None:
    """``AuditEvent`` — the append-only ledger.

    ``timestamp`` maps to the ``occurred_at`` column. The row also carries
    ``created_at``/``updated_at``, which the domain model does not have; dropping them is
    a documented decision, and this test is what stops the mapping from quietly starting
    to use ``created_at`` as the event time.
    """
    event = AuditEvent(
        id=new_id(IdKind.AUDIT_EVENT),
        actor="dana",
        actor_type=ActorType.HUMAN,
        action="approval.record",
        resource="verification_job",
        resource_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        timestamp=AWARE,
        metadata={"decision": "APPROVE", "commit_sha": "b" * 40},
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        correlation_id="corr-1",
        source_ip="203.0.113.7",
    )
    restored = to_from_domain_row(event)
    assert restored == event
    assert restored.timestamp == AWARE


def test_repository_ref_round_trips() -> None:
    """``RepositoryRef`` — the identity embedded in every job."""
    reference = RepositoryRef(full_name="acme/widgets", github_id=991, default_branch="trunk")
    assert to_domain(from_domain(reference)) == reference


def test_every_registered_mapping_round_trips_its_domain_type() -> None:
    """Every registered pair converts *something*, in both directions.

    A minimal instance is built per model type through the factories above; the value of
    this test is coverage of the registry itself, so that adding an entity without a
    round-trip test fails here rather than in production.
    """
    job_id = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0"
    samples: dict[type[object], object] = {
        VerificationJob: _job(),
        RepositoryRef: RepositoryRef(full_name="acme/widgets"),
        JobStage: JobStage(
            id="stage_1",
            job_id=job_id,
            stage=JobStatus.PREPARING,
            outcome=JobStageOutcome.SUCCEEDED,
            started_at=AWARE,
            completed_at=LATER,
            created_at=AWARE,
            updated_at=LATER,
        ),
        Finding: Finding(
            id="find_1",
            job_id=job_id,
            severity=Severity.INFO,
            category=FindingCategory.TESTS,
            title="T",
            description="D",
            repository="acme/widgets",
            origin=FindingOrigin.TOOL,
            source="pytest",
        ),
        Question: Question(
            id="qst_1",
            job_id=job_id,
            title="T",
            description="D",
            created_by="security",
        ),
        QuestionAnswer: QuestionAnswer(
            id="ans_1",
            question_id="qst_1",
            body="B",
            answered_by="dana",
        ),
        Approval: Approval(
            id="apv_1",
            job_id=job_id,
            commit_sha="b" * 40,
            decision=ApprovalDecision.APPROVE,
            decided_by="dana",
        ),
        Artifact: Artifact(
            id="art_1",
            job_id=job_id,
            kind=ArtifactKind.LOG,
            size_bytes=1,
            sha256="a" * 64,
            storage_key="sha256/aa/aa/x",
        ),
        ToolResult: ToolResult(
            id="trun_1",
            job_id=job_id,
            tool="pytest",
            command="pytest",
            status=ToolStatus.PASSED,
            exit_code=0,
            started_at=AWARE,
            completed_at=LATER,
            duration_seconds=1.0,
        ),
        AgentRun: AgentRun(
            id="arun_1",
            job_id=job_id,
            role=AgentRole.CODE_REVIEWER,
            prompt_version="v1",
        ),
        ModelRequestRecord: ModelRequestRecord(
            id="mreq_1",
            provider="openai",
            model="gpt-5",
            prompt_version="v1",
            output_tokens=1,
            status=ModelRequestStatus.SUCCEEDED,
        ),
        NotificationRecord: NotificationRecord(
            id="ntf_1",
            channel=NotificationChannel.EMAIL,
            subject="S",
            body="B",
            dedup_key="k",
        ),
        KnowledgeDocument: KnowledgeDocument(
            id="kdoc_1",
            title="T",
            body="B",
            source_type=KnowledgeSourceType.ADR,
            authority=KnowledgeAuthority.APPROVED_DOCUMENT,
        ),
        EngineeringDecision: EngineeringDecision(
            id="dec_1",
            statement="S",
            decided_by="dana",
        ),
        SandboxRun: SandboxRun(
            id="srun_1",
            job_id=job_id,
            backend=SandboxBackendKind.LOCAL,
        ),
        AuditEvent: AuditEvent(
            id="aud_1",
            actor="system",
            actor_type=ActorType.SYSTEM,
            action="job.transition",
            resource="verification_job",
            resource_id=job_id,
        ),
    }

    registered_models = {mapping.model_type for mapping in ENTITY_MAPPINGS.values()}
    assert registered_models == set(samples), (
        "every registered mapping needs a sample here, and every sample needs a mapping"
    )
    for model in samples.values():
        assert_round_trips(model)


def test_unknown_model_raises_rather_than_guessing() -> None:
    """A conversion that has no mapping fails loudly.

    ``from_domain`` returning ``None`` or a best-effort row would turn "we forgot to
    persist this" into "we persisted something wrong".
    """
    with pytest.raises(KeyError, match="no entity mapping registered"):
        from_domain(TechnologyStack(languages=["python"]))


def test_from_domain_rejects_unmapped_type() -> None:
    """The error names the offending type so the fix is obvious from the traceback."""
    with pytest.raises(KeyError) as excinfo:
        from_domain(object())
    assert "object" in str(excinfo.value)


def test_tool_invocation_serializes_to_the_invocation_column() -> None:
    """The recorded invocation keeps the exact argv that ran (spec section 21).

    A row that recorded only the friendly ``command`` string would make "what did Scout
    actually execute?" unanswerable, which is the first question in any review.
    """
    from scout_persistence.mapping import invocation_to_json, tool_run_capability

    invocation = ToolInvocation(
        tool="pytest",
        capability=Capability.TEST_RUN,
        argv=["pytest", "-q", "--maxfail=1"],
        working_directory="/var/workspaces/job_1",
        timeout_seconds=900,
        rationale="Run the suite the PR touches.",
        requested_by="orchestrator",
    )
    payload = invocation_to_json(invocation)
    assert payload["argv"] == ["pytest", "-q", "--maxfail=1"]
    assert payload["capability"] == "test.run"
    assert tool_run_capability(invocation) == "test.run"
    assert tool_run_capability(None) == Capability.TEST_RUN.value
