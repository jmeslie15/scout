"""Catalog and aggregate endpoints: repositories, tools, agents, knowledge, audit, metrics.

These are the read-mostly views behind the dashboard's secondary pages. They are separated
from the job routes because they describe the *platform* rather than one verification.

Where a dashboard view needs an aggregate that no repository method provides (a cross-job
finding count, a per-agent token total), the query is expressed directly against the table
here and noted as such. The alternative — inventing a repository method with a misleading
name — would hide the layering shortcut instead of documenting it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from scout_api.deps import UowDep
from scout_api.schemas import (
    AgentCatalogEntry,
    AuditPage,
    ErrorBody,
    KnowledgeCreateRequest,
    MetricsSummary,
    Page,
    RepositorySummaryView,
    ToolCatalogEntry,
)
from scout_domain.enums import ActorType, AgentRole, JobStatus, Severity
from scout_domain.ids import IdKind, new_id
from scout_domain.models.audit import AuditEvent
from scout_domain.models.knowledge import (
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from scout_observability import get_logger
from scout_persistence import mapping
from scout_persistence.tables import (
    AgentRunRow,
    AuditEventRow,
    FindingRow,
    JobRow,
    ModelRequestRow,
    QuestionRow,
    RepositoryRow,
    ToolRunRow,
)

__all__ = ["router"]

_log = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["catalog"])

#: What each agent role is for. Static, because the roster is fixed by design: agents do not
#: spawn other agents (spec section 40), so the set is known at build time.
_AGENT_DESCRIPTIONS: dict[str, str] = {
    AgentRole.REPOSITORY_ANALYZER.value: (
        "Builds a machine-readable understanding of the repository: services, entry points, "
        "API surfaces, and architecture boundaries."
    ),
    AgentRole.CODE_REVIEWER.value: (
        "Reviews the change for correctness, maintainability, API behaviour, data handling, "
        "concurrency, error handling, and test quality."
    ),
    AgentRole.SECURITY.value: (
        "Interprets deterministic security scanner output in context and inspects "
        "authentication, authorization, input validation, and secret handling."
    ),
    AgentRole.TEST_ANALYSIS.value: (
        "Distinguishes 'the tests passed' from 'the tests cover the change', and identifies "
        "changed behaviour that no test exercises."
    ),
    AgentRole.FEATURE_VERIFICATION.value: (
        "Extracts acceptance criteria and produces an executable verification plan with "
        "recorded evidence for each criterion."
    ),
    AgentRole.INVESTIGATION.value: (
        "Resolves uncertainty in the documented order: evidence, repository inspection, "
        "tests, documentation, engineering memory, reasoning, and only then a human question."
    ),
    AgentRole.REPORT_GENERATOR.value: (
        "Aggregates stage results into the structured verification report. It renders what "
        "was observed; it does not decide the recommendation."
    ),
}


@router.get(
    "/repositories",
    response_model=Page[RepositorySummaryView],
    summary="List registered repositories",
)
async def list_repositories(
    uow: UowDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RepositorySummaryView]:
    """List repositories Scout has seen, with their job volume."""
    rows = await uow.repositories.list_all(limit=limit + offset)
    page = rows[offset : offset + limit]

    items: list[RepositorySummaryView] = []
    for row in page:
        aggregate = await uow.session.execute(
            select(func.count(JobRow.id), func.max(JobRow.created_at)).where(
                JobRow.repository_full_name == row.full_name
            )
        )
        job_count, last_job_at = aggregate.one()
        items.append(
            RepositorySummaryView(
                full_name=row.full_name,
                github_id=row.github_id,
                default_branch=row.default_branch or "main",
                is_active=bool(row.is_active),
                job_count=int(job_count or 0),
                last_job_at=last_job_at,
            )
        )
    total = int(await uow.session.scalar(select(func.count()).select_from(RepositoryRow)) or 0)
    return Page[RepositorySummaryView](items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/repositories/{owner}/{name}",
    response_model=RepositorySummaryView,
    responses={404: {"model": ErrorBody}},
    summary="Repository detail",
)
async def get_repository(owner: str, name: str, uow: UowDep) -> RepositorySummaryView:
    """Return one repository."""
    full_name = f"{owner}/{name}"
    row = await uow.repositories.get_by_full_name(full_name)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "repository_not_found", "message": f"no repository {full_name}"},
        )
    aggregate = await uow.session.execute(
        select(func.count(JobRow.id), func.max(JobRow.created_at)).where(
            JobRow.repository_full_name == full_name
        )
    )
    job_count, last_job_at = aggregate.one()
    return RepositorySummaryView(
        full_name=row.full_name,
        github_id=row.github_id,
        default_branch=row.default_branch or "main",
        is_active=bool(row.is_active),
        job_count=int(job_count or 0),
        last_job_at=last_job_at,
    )


@router.get("/tools", response_model=Page[ToolCatalogEntry], summary="List deterministic tools")
async def list_tools(uow: UowDep) -> Page[ToolCatalogEntry]:
    """List the deterministic tool roster with real usage statistics.

    The roster comes from the registry (so a tool with no runs still appears, which is how an
    operator notices that a tool is never applicable), and the statistics come from recorded
    runs.
    """
    entries: list[ToolCatalogEntry] = []
    try:
        from scout_tools.registry import default_registry

        registry = default_registry()
        for tool in registry:
            aggregate = await uow.session.execute(
                select(
                    func.count(ToolRunRow.id),
                    func.avg(ToolRunRow.duration_seconds),
                ).where(ToolRunRow.tool == tool.name)
            )
            runs, avg_duration = aggregate.one()
            failures = int(
                await uow.session.scalar(
                    select(func.count())
                    .select_from(ToolRunRow)
                    .where(ToolRunRow.tool == tool.name, ToolRunRow.status == "failed")
                )
                or 0
            )
            entries.append(
                ToolCatalogEntry(
                    name=tool.name,
                    category=tool.category.value,
                    description=tool.description or "",
                    capability=tool.capability.value,
                    runs=int(runs or 0),
                    failures=failures,
                    avg_duration_seconds=float(avg_duration) if avg_duration is not None else None,
                )
            )
    except Exception as exc:
        _log.error("the tool registry could not be enumerated", error=str(exc), exc_info=True)
    return Page[ToolCatalogEntry](
        items=entries, total=len(entries), limit=len(entries) or 1, offset=0
    )


@router.get("/agents", response_model=Page[AgentCatalogEntry], summary="List reasoning agents")
async def list_agents(uow: UowDep) -> Page[AgentCatalogEntry]:
    """List the fixed agent roster with run, latency, and cost totals.

    Token and cost totals are read from ``model_requests`` rather than ``agent_runs``, because
    the gateway records one row per *attempt* and a retried call must be counted twice for the
    cost figure to be honest.
    """
    entries: list[AgentCatalogEntry] = []
    for role in AgentRole:
        runs = int(
            await uow.session.scalar(
                select(func.count()).select_from(AgentRunRow).where(AgentRunRow.role == role.value)
            )
            or 0
        )
        latency = await uow.session.scalar(
            select(func.avg(AgentRunRow.latency_seconds)).where(AgentRunRow.role == role.value)
        )
        usage = await uow.session.execute(
            select(
                func.coalesce(func.sum(ModelRequestRow.input_tokens), 0),
                func.coalesce(func.sum(ModelRequestRow.output_tokens), 0),
                func.coalesce(func.sum(ModelRequestRow.estimated_cost_usd), 0.0),
            ).where(ModelRequestRow.agent == role.value)
        )
        input_tokens, output_tokens, cost = usage.one()

        prompt_versions: list[str] = []
        try:
            from pathlib import Path

            prompts_root = Path(__file__).resolve().parents[4] / "prompts" / role.value
            if prompts_root.is_dir():
                prompt_versions = sorted(path.stem for path in prompts_root.glob("v*.md"))
        except Exception:
            prompt_versions = []

        entries.append(
            AgentCatalogEntry(
                role=role.value,
                description=_AGENT_DESCRIPTIONS.get(role.value, ""),
                capabilities=[capability.value for capability in _capabilities_for(role)],
                prompt_versions=prompt_versions,
                runs=runs,
                avg_latency_seconds=float(latency) if latency is not None else None,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                estimated_cost_usd=float(cost or 0.0),
            )
        )
    return Page[AgentCatalogEntry](
        items=entries, total=len(entries), limit=len(entries) or 1, offset=0
    )


@router.get(
    "/knowledge",
    response_model=Page[KnowledgeDocument],
    summary="Search engineering memory",
)
async def list_knowledge(
    uow: UowDep,
    scope: Annotated[str | None, Query(max_length=200)] = None,
    query: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[KnowledgeDocument]:
    """Search engineering memory by keyword, scoped to a repository."""
    rows = await uow.knowledge.list_active_for_scope(scope or "*")
    documents = [mapping.to_knowledge_document(row) for row in rows]

    if query:
        needle = query.lower()
        documents = [
            document
            for document in documents
            if needle in document.title.lower() or needle in document.body.lower()
        ]

    # Human decisions outrank inferred knowledge; recency breaks ties.
    documents.sort(
        key=lambda document: (document.authority_rank, document.created_at), reverse=True
    )
    page = documents[:limit]
    return Page[KnowledgeDocument](items=page, total=len(documents), limit=limit, offset=0)


@router.post(
    "/knowledge",
    response_model=KnowledgeDocument,
    status_code=status.HTTP_201_CREATED,
    summary="Record engineering memory",
)
async def create_knowledge(body: KnowledgeCreateRequest, uow: UowDep) -> KnowledgeDocument:
    """Record a knowledge document or an engineering decision.

    Recording knowledge is a privileged action: it changes what every future job will believe
    about the system, so it is audited (spec section 43).
    """
    try:
        source_type = KnowledgeSourceType(body.source_type)
        authority = KnowledgeAuthority(body.authority)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_knowledge_enum", "message": str(exc)},
        ) from exc

    document = KnowledgeDocument(
        id=new_id(IdKind.KNOWLEDGE_DOCUMENT),
        title=body.title,
        body=body.body,
        source_type=source_type,
        authority=authority,
        scope=body.scope,
        tags=body.tags,
    )
    try:
        row = await uow.knowledge.upsert(document)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "knowledge_rejected", "message": str(exc)},
        ) from exc

    await uow.audit_events.append(
        AuditEvent(
            id=new_id(IdKind.AUDIT_EVENT),
            actor="operator",
            actor_type=ActorType.HUMAN,
            action="knowledge.record",
            resource="knowledge_document",
            resource_id=document.id,
            metadata={
                "source_type": document.source_type.value,
                "authority": document.authority.value,
                "scope": document.scope,
            },
        )
    )
    return mapping.to_knowledge_document(row)


@router.get("/audit", response_model=AuditPage, summary="Audit log")
async def list_audit(
    uow: UowDep,
    job_id: Annotated[str | None, Query(max_length=100)] = None,
    resource: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditPage:
    """Return audit events, newest first. Read-only by construction: there is no write path."""
    statement = select(AuditEventRow)
    count_statement = select(func.count()).select_from(AuditEventRow)
    if job_id:
        statement = statement.where(AuditEventRow.job_id == job_id)
        count_statement = count_statement.where(AuditEventRow.job_id == job_id)
    if resource:
        statement = statement.where(AuditEventRow.resource == resource)
        count_statement = count_statement.where(AuditEventRow.resource == resource)

    total = int(await uow.session.scalar(count_statement) or 0)
    rows = list(
        await uow.session.scalars(
            statement.order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return AuditPage(
        items=[mapping.to_audit_event(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/metrics/summary", response_model=MetricsSummary, summary="Aggregate metrics")
async def metrics_summary(uow: UowDep) -> MetricsSummary:
    """Aggregate the metrics the dashboard landing page shows (spec section 49)."""
    by_status = await uow.jobs.count_by_status()
    jobs_total = sum(by_status.values())
    jobs_failed = by_status.get(JobStatus.FAILED, 0)

    severity_rows = await uow.session.execute(
        select(FindingRow.severity, func.count(FindingRow.id)).group_by(FindingRow.severity)
    )
    severity_counts = {Severity(severity): int(count) for severity, count in severity_rows.all()}

    escalations = int(
        await uow.session.scalar(
            select(func.count()).select_from(QuestionRow).where(QuestionRow.status != "OPEN")
        )
        or 0
    )
    llm = await uow.session.execute(
        select(
            func.coalesce(func.sum(ModelRequestRow.input_tokens), 0),
            func.coalesce(func.sum(ModelRequestRow.output_tokens), 0),
            func.coalesce(func.sum(ModelRequestRow.estimated_cost_usd), 0.0),
        )
    )
    input_tokens, output_tokens, cost = llm.one()

    avg_duration = await uow.session.scalar(
        select(func.avg(JobRow.max_attempts)).where(JobRow.completed_at.is_not(None))
    )

    test_failures = int(
        await uow.session.scalar(
            select(func.count())
            .select_from(ToolRunRow)
            .where(ToolRunRow.status == "failed", ToolRunRow.tool.like("%test%"))
        )
        or 0
    )

    return MetricsSummary(
        jobs_total=jobs_total,
        jobs_failed=jobs_failed,
        jobs_by_status={status.value: count for status, count in by_status.items()},
        findings_by_severity={severity: severity_counts.get(severity, 0) for severity in Severity},
        human_escalations=escalations,
        llm_tokens=int(input_tokens or 0) + int(output_tokens or 0),
        llm_cost_usd=float(cost or 0.0),
        avg_job_duration_seconds=float(avg_duration) if avg_duration is not None else None,
        test_failures=test_failures,
    )


def _capabilities_for(role: AgentRole) -> list[Any]:
    """Return the capabilities each agent role is granted (spec section 54).

    Least privilege is expressed here so that the dashboard can display it and so that a
    reviewer can see, in one place, that the report generator cannot run tools and the
    security agent cannot write to GitHub.
    """
    from scout_domain.enums import Capability

    mapping_by_role: dict[AgentRole, list[Capability]] = {
        AgentRole.REPOSITORY_ANALYZER: [
            Capability.REPOSITORY_READ,
            Capability.REPOSITORY_SEARCH,
            Capability.GIT_DIFF,
        ],
        AgentRole.CODE_REVIEWER: [
            Capability.REPOSITORY_READ,
            Capability.REPOSITORY_SEARCH,
            Capability.GIT_DIFF,
            Capability.KNOWLEDGE_SEARCH,
        ],
        AgentRole.SECURITY: [
            Capability.REPOSITORY_READ,
            Capability.REPOSITORY_SEARCH,
            Capability.SECURITY_SCAN,
            Capability.DEPENDENCY_SCAN,
            Capability.KNOWLEDGE_SEARCH,
        ],
        AgentRole.TEST_ANALYSIS: [
            Capability.REPOSITORY_READ,
            Capability.TEST_RUN,
            Capability.TEST_GENERATE,
            Capability.KNOWLEDGE_SEARCH,
        ],
        AgentRole.FEATURE_VERIFICATION: [
            Capability.REPOSITORY_READ,
            Capability.TEST_RUN,
            Capability.TEST_GENERATE,
            Capability.KNOWLEDGE_SEARCH,
        ],
        AgentRole.INVESTIGATION: [
            Capability.REPOSITORY_READ,
            Capability.REPOSITORY_SEARCH,
            Capability.GIT_DIFF,
            Capability.KNOWLEDGE_SEARCH,
        ],
        AgentRole.REPORT_GENERATOR: [Capability.KNOWLEDGE_SEARCH],
    }
    return list(mapping_by_role.get(role, []))
