"""PREPARING: materialise the pinned revision and describe the change.

Spec section 13 is emphatic that Scout must never analyse an unpinned moving branch, so this
stage is where the commit range becomes real: git fetches the two SHAs, checks out the head,
and every later stage works from that checkout and nothing else.

What it produces, and why each piece is persisted rather than kept in memory:

* the pinned :class:`~scout_domain.models.repository.ChangeSet` and a git-compatible unified
  diff (``DIFF`` artifact) -- the evidence every later claim rests on;
* the changed-path list (``CHANGED_PATHS`` artifact) -- cheap for a report or a dashboard to
  re-read without parsing a diff;
* the repository configuration snapshot (``CONFIG_SNAPSHOT``) -- kept verbatim, because what
  Scout was *asked* to do is part of the record even when the file is malformed;
* the :class:`~scout_orchestrator.stages.prepared.PreparedState` document
  (``REPOSITORY_SUMMARY``) -- everything a resumed job needs to avoid repeating the checkout.

A malformed ``.scout/engineering.yaml`` is deliberately *not* a job failure (spec section 14
asks for a clear finding instead): Scout falls back to its conservative defaults, records a
``CONFIGURATION`` finding with the parse error in its evidence, and carries on. The severity
is MEDIUM because the verification still ran, with defaults that are stricter than most
repositories ask for; the finding is what tells a human their configuration was ignored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from scout_domain.config import CONFIG_PATH, ScoutRepositoryConfig, parse_repository_config
from scout_domain.enums import EvidenceKind, FindingCategory, JobStatus, Severity
from scout_domain.errors import ConfigurationError, RepositoryConfigurationError
from scout_domain.ids import IdKind, new_id
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.finding import Finding, FindingOrigin
from scout_domain.models.report import ReportSection, ReportSectionStatus
from scout_domain.models.repository import (
    ChangeSet,
    DetectedService,
    RepositorySummary,
    TechnologyStack,
)
from scout_domain.risk import classify_change_set
from scout_github import build_change_set, extract_requirement_text, render_unified_diff
from scout_observability import get_logger
from scout_orchestrator.errors import StageExecutionError, StageInfrastructureError
from scout_orchestrator.settings import parse_stage_names
from scout_orchestrator.stages.context import BaseStage, StageContext, StageResult
from scout_orchestrator.stages.gitdiff import build_diff_argv, parse_git_diff, render_path_list
from scout_orchestrator.stages.prepared import PreparedState
from scout_sandbox import GitRunner, ensure_git_available, repo_url_for
from scout_tools.detection import detect_layout, detect_technology_stack, guess_service_roots

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scout_sandbox import Workspace
    from scout_tools.detection import DetectedLayout

__all__ = ["PreparingStage"]

_log = get_logger(__name__)

#: The status this stage runs under. A module constant so the error paths below do not need a
#: bound instance to name the stage they failed in.
_STAGE: Final = JobStatus.PREPARING

#: Longest configuration file Scout will read. A configuration file larger than this is not a
#: configuration file.
_MAX_CONFIG_BYTES: Final = 512_000

#: Ceiling on the unified diff captured into memory. The sandbox truncates a larger capture
#: itself and records that it did, rather than letting a huge diff exhaust the worker.
_MAX_DIFF_BYTES: Final = 64 * 1024 * 1024

#: Filenames that indicate an application entry point.
_ENTRY_POINT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "main.py",
        "__main__.py",
        "app.py",
        "asgi.py",
        "wsgi.py",
        "manage.py",
        "server.py",
        "cli.py",
        "index.ts",
        "index.js",
        "main.ts",
        "server.ts",
        "main.go",
    }
)

#: Path fragments that indicate an HTTP/API surface consumed by others.
_API_SURFACE_FRAGMENTS: Final[tuple[str, ...]] = (
    "api/",
    "apis/",
    "routes/",
    "routers/",
    "controllers/",
    "endpoints/",
    "handlers/",
    "graphql/",
    "proto/",
    "openapi",
)

#: Path fragments that indicate authentication or authorization code. Detected from paths
#: rather than from content because this feeds the report narrative, not a verdict.
_AUTH_FRAGMENTS: Final[tuple[str, ...]] = (
    "auth",
    "login",
    "session",
    "oauth",
    "jwt",
    "rbac",
    "permission",
    "acl",
)

#: Ceiling on the number of paths listed per narrative bucket, so a 900-file change does not
#: produce a 900-line section.
_MAX_LISTED: Final = 20


class PreparingStage(BaseStage):
    """Create the workspace's pinned checkout and classify the change."""

    stage = _STAGE

    async def run(self, ctx: StageContext) -> StageResult:
        """Materialise the revision, describe it, and persist the description."""
        git = GitRunner(ctx.sandbox)
        await ensure_git_available(ctx.sandbox)

        checkout = await git.materialize(
            ctx.workspace,
            repo_url=repo_url_for(ctx.job.repository_full_name, base_url=ctx.settings.git_base_url),
            base_sha=ctx.job.base_sha,
            head_sha=ctx.job.head_sha,
            token=ctx.settings.git_token,
        )
        _log.info(
            "materialised pinned revision",
            job_id=ctx.job.id,
            head_sha=checkout.head_sha,
            changed_file_count=checkout.file_count,
        )

        layout = detect_layout(ctx.workspace)
        stack = detect_technology_stack(ctx.workspace, layout=layout)
        summary = _build_repository_summary(
            ctx, layout=layout, stack=stack, head_sha=checkout.head_sha
        )

        config, config_raw, config_errors = _load_repository_config(ctx.workspace)

        raw_diff = await _read_git_diff(ctx, checkout.base_sha, checkout.head_sha)
        diff_entries = parse_git_diff(raw_diff)
        if not diff_entries:
            # An empty three-dot diff is information, not an error: a force-push race and an
            # already-merged branch both produce one. It is recorded, and the risk default
            # decides the depth, rather than crashing a job that can still be reported on.
            _log.warning("the pinned commit range contains no changes", job_id=ctx.job.id)

        diff_artifact = ctx.artifacts.put_text(
            render_unified_diff(diff_entries),
            kind=ArtifactKind.DIFF,
            content_type="text/x-diff",
            filename="changes.diff",
            job_id=ctx.job.id,
            sandbox_run_id=ctx.sandbox_run_id,
        )
        paths_artifact = ctx.artifacts.put_text(
            render_path_list([str(entry["filename"]) for entry in diff_entries]),
            kind=ArtifactKind.CHANGED_PATHS,
            content_type="text/plain",
            filename="changed-paths.txt",
            job_id=ctx.job.id,
            sandbox_run_id=ctx.sandbox_run_id,
        )
        config_artifact = ctx.artifacts.put_text(
            config_raw,
            kind=ArtifactKind.CONFIG_SNAPSHOT,
            content_type="application/yaml",
            filename="engineering.yaml",
            job_id=ctx.job.id,
            sandbox_run_id=ctx.sandbox_run_id,
        )

        change_set = build_change_set(
            checkout.base_sha,
            checkout.head_sha,
            diff_entries,
            diff_artifact_id=diff_artifact.id,
        ).evolve(changed_paths_artifact_id=paths_artifact.id)

        risk = classify_change_set(change_set, default_risk=config.verification.default_risk)

        pr_body, commit_messages, requirement_text = await _read_requirements(
            ctx, base_sha=checkout.base_sha, head_sha=checkout.head_sha, git=git
        )

        state_artifact = ctx.artifacts.put_text(
            PreparedState(
                change_set=change_set,
                repository_summary=summary,
                risk=risk,
                repository_config=config,
                config_raw=config_raw,
                pr_body=pr_body,
                requirement_text=requirement_text,
                commit_messages=commit_messages,
            ).to_text(),
            kind=ArtifactKind.REPOSITORY_SUMMARY,
            content_type="application/json",
            filename="prepared-state.json",
            job_id=ctx.job.id,
            sandbox_run_id=ctx.sandbox_run_id,
        )

        findings = tuple(
            _config_finding(ctx, artifact_id=config_artifact.id, detail=detail)
            for detail in config_errors
        )

        return StageResult.succeeded_for(
            self.stage,
            narrative=(
                f"checked out {checkout.head_sha[:12]} and classified "
                f"{len(change_set.files)} changed file(s) as {risk.level.value} risk"
            ),
            findings=findings,
            artifacts=ctx.artifacts.take(),
            sections=_preparation_sections(
                change_set=change_set,
                risk_explanation=risk.explain(),
                stack=stack,
                diff_artifact_id=diff_artifact.id,
                state_artifact_id=state_artifact.id,
            ),
            payload={
                "risk_level": risk.level.value,
                "changed_paths": list(change_set.changed_paths),
                "changed_file_count": len(change_set.files),
                "head_sha": checkout.head_sha,
                "base_sha": checkout.base_sha,
                "prepared_artifact_id": state_artifact.id,
                "config_valid": not config_errors,
            },
        )


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def _load_repository_config(
    workspace: Workspace,
) -> tuple[ScoutRepositoryConfig, str, list[str]]:
    """Read and parse ``.scout/engineering.yaml`` without ever raising for repository content.

    Returns:
        ``(config, raw_text, errors)``. A parse failure yields Scout's documented fallback
        configuration plus the error text, which the caller turns into a finding.
    """
    raw_text = _read_config_text(workspace)
    errors: list[str] = []
    try:
        config = parse_repository_config(raw_text)
    except RepositoryConfigurationError as exc:
        errors.append(str(exc))
        config = parse_repository_config(None)

    errors.extend(_validate_stage_overrides(config))
    return config, raw_text, errors


def _read_config_text(workspace: Workspace) -> str:
    """Read the configuration file verbatim, tolerating absence and unreadability."""
    try:
        if not workspace.exists(CONFIG_PATH):
            return ""
        return workspace.read_text(CONFIG_PATH, max_bytes=_MAX_CONFIG_BYTES)
    except Exception as exc:
        _log.warning("could not read repository configuration", path=CONFIG_PATH, error=str(exc))
        return ""


def _validate_stage_overrides(config: ScoutRepositoryConfig) -> list[str]:
    """Report stage names in the repository configuration that are not job statuses.

    A typo in ``disabled_stages`` silently protects nothing, which is exactly the kind of
    configuration error spec section 14 wants surfaced as a finding rather than accepted.
    """
    errors: list[str] = []
    for field, raw in (
        ("verification.required_stages", config.verification.required_stages),
        ("verification.disabled_stages", config.verification.disabled_stages),
    ):
        if not raw:
            continue
        try:
            parse_stage_names(",".join(raw), variable=f".scout/engineering.yaml {field}")
        except ConfigurationError as exc:
            errors.append(str(exc))
    return errors


def _config_finding(ctx: StageContext, *, artifact_id: str, detail: str) -> Finding:
    """Build the ``CONFIGURATION`` finding for a repository configuration problem.

    ``origin=TOOL`` is the honest classification: the finding is produced by Scout's own
    deterministic parser and is backed by the verbatim configuration artifact, so it records
    a machine-verified fact rather than an inference.
    """
    return Finding(
        id=new_id(IdKind.FINDING),
        job_id=ctx.job.id,
        severity=Severity.MEDIUM,
        category=FindingCategory.CONFIGURATION,
        title=".scout/engineering.yaml could not be applied",
        description=(
            f"{detail} Scout continued with its conservative defaults, so the verification "
            "depth may not be what this repository intended."
        ),
        repository=ctx.job.repository_full_name,
        file=CONFIG_PATH,
        evidence=[
            EvidenceReference(
                kind=EvidenceKind.ARTIFACT,
                reference_id=artifact_id,
                summary="the configuration file exactly as it existed at the verified commit",
                file_path=CONFIG_PATH,
                artifact_id=artifact_id,
            )
        ],
        recommendation=(
            "Correct .scout/engineering.yaml; Scout reports configuration problems rather "
            "than silently ignoring the file."
        ),
        confidence=1.0,
        origin=FindingOrigin.TOOL,
        source="config_parser",
    )


# --------------------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------------------


async def _read_git_diff(ctx: StageContext, base_sha: str, head_sha: str) -> str:
    """Run the three-dot diff and return its text, or fail loudly."""
    outcome = await ctx.sandbox.run_simple(
        *build_diff_argv(base_sha, head_sha), cwd=".", label="git-diff"
    )
    if not outcome.launched or outcome.timed_out:
        raise StageInfrastructureError(
            "the git diff command did not complete",
            stage=_STAGE,
            detail=outcome.describe_failure(),
        )
    if outcome.exit_code != 0:
        raise StageExecutionError(
            "git diff failed for the pinned commit range",
            stage=_STAGE,
            exit_code=outcome.exit_code,
            stderr=outcome.read_stderr(limit_bytes=2000),
        )
    return outcome.read_stdout(limit_bytes=_MAX_DIFF_BYTES)


async def _read_requirements(
    ctx: StageContext,
    *,
    base_sha: str,
    head_sha: str,
    git: GitRunner,
) -> tuple[str, tuple[str, ...], str]:
    """Read the PR description and commit messages, and extract the requirement material.

    The PR description is read from GitHub because that is where the author wrote it. A
    missing pull request is a genuine failure: verification without the stated intent would be
    exactly the "the tests passed, therefore the feature works" fallacy spec section 23 names.
    """
    pull_request = await ctx.github.get_pull_request(
        ctx.job.repository_full_name, ctx.job.pull_request_number
    )
    pr_body = pull_request.body or ""
    commit_messages = tuple(await git.log(ctx.workspace, base_sha, head_sha))

    requirement_text = extract_requirement_text(pr_body or None, [], list(commit_messages))
    if pr_body:
        ctx.artifacts.put_text(
            pr_body,
            kind=ArtifactKind.PR_DESCRIPTION,
            content_type="text/markdown",
            filename="pull-request-description.md",
            job_id=ctx.job.id,
            sandbox_run_id=ctx.sandbox_run_id,
        )
    if requirement_text:
        ctx.artifacts.put_text(
            requirement_text,
            kind=ArtifactKind.REQUIREMENTS,
            content_type="text/markdown",
            filename="requirements.md",
            job_id=ctx.job.id,
            sandbox_run_id=ctx.sandbox_run_id,
        )
    return pr_body, commit_messages, requirement_text


# --------------------------------------------------------------------------------------
# Repository summary
# --------------------------------------------------------------------------------------


def _build_repository_summary(
    ctx: StageContext,
    *,
    layout: DetectedLayout,
    stack: TechnologyStack,
    head_sha: str,
) -> RepositorySummary:
    """Build the deterministic repository summary (spec section 15).

    Everything here is read from file names and manifests. The repository analyzer agent may
    add architecture narrative later, but the structure Scout reasons about is produced
    deterministically so that two runs over the same commit agree.
    """
    services = _detect_services(layout)
    auth_surfaces = _matching_files(layout, frozenset(), fragment=_AUTH_FRAGMENTS)

    return RepositorySummary(
        repository=ctx.job.repository,
        commit_sha=head_sha,
        stack=stack,
        services=services,
        file_count=len(layout.all_files),
        total_bytes=ctx.workspace.total_size_bytes(),
        entry_points=_matching_files(layout, _ENTRY_POINT_NAMES, fragment=None),
        api_surfaces=_matching_files(layout, frozenset(), fragment=_API_SURFACE_FRAGMENTS),
        auth_mechanisms=auth_surfaces,
        authorization_mechanisms=auth_surfaces,
        documentation_paths=list(layout.documentation_roots),
        infrastructure_paths=list(layout.infra_roots),
        test_paths=list(layout.test_roots),
        migration_paths=list(layout.migration_roots),
        notes=_summary_notes(layout, stack, services),
    )


def _detect_services(layout: DetectedLayout) -> list[DetectedService]:
    """Turn candidate service roots into detected services."""
    services: list[DetectedService] = []
    for root in guess_service_roots(layout):
        if root == ".":
            services.append(
                DetectedService(
                    name="repository",
                    path=".",
                    kind="application",
                    has_tests=bool(layout.test_roots),
                )
            )
            continue
        services.append(
            DetectedService(
                name=root.rsplit("/", 1)[-1],
                path=root,
                kind="service",
                has_tests=any(
                    test_root == root or test_root.startswith(f"{root}/")
                    for test_root in layout.test_roots
                ),
            )
        )
    return services


def _matching_files(
    layout: DetectedLayout,
    names: frozenset[str],
    *,
    fragment: Sequence[str] | None,
) -> list[str]:
    """Return repository files matching a name set or a list of path fragments."""
    matched: list[str] = []
    for path in layout.all_files:
        basename = path.rsplit("/", 1)[-1]
        if (names and basename in names) or (fragment and any(part in path for part in fragment)):
            matched.append(path)
        if len(matched) >= _MAX_LISTED:
            break
    return matched


def _summary_notes(
    layout: DetectedLayout,
    stack: TechnologyStack,
    services: list[DetectedService],
) -> list[str]:
    """Compose short, factual notes for the report narrative."""
    notes = [
        f"{len(layout.all_files)} tracked file(s)",
        f"languages: {', '.join(stack.languages) or 'unknown'}",
        f"{len(services)} service root(s)",
    ]
    if layout.monorepo:
        notes.append("layout looks like a monorepo (apps/packages/services/libs present)")
    if stack.containerized:
        notes.append("container configuration detected")
    if stack.has_infrastructure_as_code:
        notes.append("infrastructure-as-code detected")
    if not layout.test_roots:
        notes.append("no test root detected")
    return notes


def _preparation_sections(
    *,
    change_set: ChangeSet,
    risk_explanation: str,
    stack: TechnologyStack,
    diff_artifact_id: str,
    state_artifact_id: str,
) -> tuple[ReportSection, ...]:
    """Render the preparation record as report sections."""
    return (
        _section(
            "preparing.change_set",
            "Pinned change set",
            (
                f"{len(change_set.files)} file(s) changed between "
                f"{change_set.base_sha[:12]} and {change_set.head_sha[:12]}"
            ),
            details=[
                f"{file.change_type.value}: {file.path}" for file in change_set.files[:_MAX_LISTED]
            ],
            artifact_id=diff_artifact_id,
        ),
        _section(
            "preparing.risk",
            "Risk classification",
            risk_explanation,
            artifact_id=state_artifact_id,
        ),
        _section(
            "preparing.stack",
            "Detected technology stack",
            ", ".join(stack.languages) or "no language signals detected",
            details=[
                f"package managers: {', '.join(stack.package_managers) or 'none'}",
                f"test frameworks: {', '.join(stack.test_frameworks) or 'none'}",
                f"ci systems: {', '.join(stack.ci_systems) or 'none'}",
                f"containerized: {stack.containerized}",
            ],
            artifact_id=state_artifact_id,
        ),
    )


def _section(
    key: str,
    title: str,
    summary: str,
    *,
    details: Sequence[str] = (),
    artifact_id: str | None = None,
) -> ReportSection:
    """Build an INFO report section for the preparation record."""
    evidence = []
    if artifact_id is not None:
        evidence.append(
            EvidenceReference(
                kind=EvidenceKind.ARTIFACT,
                reference_id=artifact_id,
                summary=f"{title} evidence",
                artifact_id=artifact_id,
            )
        )
    return ReportSection(
        key=key[:100],
        title=title[:200],
        status=ReportSectionStatus.INFO,
        summary=summary[:8000],
        details=[detail[:2000] for detail in details],
        evidence=evidence,
    )
