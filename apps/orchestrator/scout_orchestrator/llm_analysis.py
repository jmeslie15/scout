"""Model-assisted repository analysis, with a deterministic floor underneath it.

Spec section 15 asks the Repository Analyzer to explain entry points, architecture
boundaries, and intent. Most of that is deterministically observable and is already produced
by :func:`scout_tools.detection.detect_technology_stack`; what a model adds is prose about
*why* the structure is shaped the way it is, which is genuinely useful and genuinely not
evidence.

Hence the shape of this module:

* The deterministic narrative is always built, and is the answer whenever anything at all is
  wrong with the model path. It is never invented and never embellished.
* The model is used only when the gateway is importable, constructible, and has the requested
  prompt template on disk. A missing prompt tree is the normal state in this repository, and
  it must degrade quietly rather than fail a verification.
* Whatever happens, the returned narrative states which of the two produced it. A reader can
  therefore never mistake deterministic detection for model output, or the reverse. Spec
  section 3.1 ("AI is not a source of truth") is the reason that honesty is structural
  rather than a convention.

The LLM packages are imported inside the function on purpose: ``scout_llm`` is allowed to be
absent (it is listed as an incomplete workstream), and a module-level import would make the
orchestrator unimportable without it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from scout_domain.models.base import utcnow
from scout_observability import get_logger

if TYPE_CHECKING:
    from scout_domain.models.agent import AgentRun
    from scout_domain.models.job import VerificationJob
    from scout_domain.models.repository import ChangeSet, RepositorySummary
    from scout_orchestrator.stages.context import StageSettings

__all__ = ["AnalysisNarrative", "analyse_repository", "deterministic_narrative"]

_log = get_logger(__name__)

#: Ceiling on the deterministic narrative, matching the report field it feeds.
_MAX_NARRATIVE: Final = 8000


@dataclass(frozen=True, slots=True)
class AnalysisNarrative:
    """The architecture narrative, plus an honest account of where it came from."""

    text: str
    used_model: bool
    note: str
    prompt_version: str | None = None
    provider: str | None = None
    model: str | None = None
    agent_run: AgentRun | None = None

    @property
    def provenance(self) -> dict[str, str]:
        """The ``agent_provenance`` entries this narrative contributes (spec section 39)."""
        if not self.used_model or self.prompt_version is None:
            return {}
        return {
            "repository_analyzer.prompt_version": self.prompt_version,
            "repository_analyzer.provider": self.provider or "unknown",
            "repository_analyzer.model": self.model or "unknown",
        }


async def analyse_repository(
    *,
    settings: StageSettings,
    job: VerificationJob,
    summary: RepositorySummary,
    change_set: ChangeSet,
) -> AnalysisNarrative:
    """Produce the architecture narrative, preferring the model when it is usable.

    Never raises: an architecture narrative is a convenience, and failing a verification
    because a model provider was unreachable would violate the separation between evidence
    and reasoning that the whole report rests on.
    """
    deterministic = deterministic_narrative(summary=summary, change_set=change_set)

    if not settings.llm_analysis_enabled:
        return AnalysisNarrative(
            text=deterministic,
            used_model=False,
            note=(
                "produced deterministically from the detected stack and repository layout; "
                "model assistance is disabled by configuration"
            ),
        )

    try:
        from scout_llm import PromptLibrary, build_gateway_from_env, default_prompts_root
    except ImportError as exc:
        _log.info("llm gateway unavailable; using the deterministic narrative", error=str(exc))
        return AnalysisNarrative(
            text=deterministic,
            used_model=False,
            note=(
                "produced deterministically from the detected stack and repository layout; "
                "the model gateway is not installed"
            ),
        )

    context_values = _prompt_variables(summary=summary, change_set=change_set)
    try:
        # The template, not this module, decides which variables it needs. Supplying extras is an
        # error in the prompt library -- it would hide a template referencing a variable nobody
        # provides -- so the declared placeholders are the contract, and a placeholder this module
        # cannot fill becomes an empty string rather than a render failure.
        role = _analyser_role()
        template = PromptLibrary(default_prompts_root()).load(
            role, settings.llm_analyzer_prompt_version
        )
        variables = {name: context_values.get(name, "") for name in template.placeholders}
        gateway = build_gateway_from_env()
        result = await gateway.complete(
            role,
            settings.llm_analyzer_prompt_version,
            variables,
            job_id=job.id,
        )
    except Exception as exc:
        _log.info(
            "repository analysis fell back to the deterministic narrative",
            job_id=job.id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return AnalysisNarrative(
            text=deterministic,
            used_model=False,
            note=(
                "produced deterministically from the detected stack and repository layout; "
                f"the model call was not usable ({type(exc).__name__})"
            ),
        )

    text = (result.text or "").strip()
    if not text:
        return AnalysisNarrative(
            text=deterministic,
            used_model=False,
            note=(
                "produced deterministically from the detected stack and repository layout; "
                "the model returned no text"
            ),
        )

    record = result.record
    return AnalysisNarrative(
        text=text[:_MAX_NARRATIVE],
        used_model=True,
        note=f"model-assisted narrative from {record.provider}/{record.model}",
        prompt_version=result.prompt_version,
        provider=record.provider,
        model=record.model,
        agent_run=_agent_run(job=job, result=result, variables=variables),
    )


def _analyser_role() -> Any:
    """Return the repository-analyzer agent role.

    Resolved through the domain enum, which is always importable, so this function cannot be
    the reason a model call fails.
    """
    from scout_domain.enums import AgentRole

    return AgentRole.REPOSITORY_ANALYZER


def _agent_run(
    *,
    job: VerificationJob,
    result: Any,
    variables: dict[str, str],
) -> AgentRun:
    """Build the reproducible record of one analyzer invocation (spec sections 32, 39)."""
    from scout_domain.enums import AgentRole, AgentStatus
    from scout_domain.ids import IdKind, new_id
    from scout_domain.models.agent import AgentRun

    record = result.record
    completed_at = utcnow()
    return AgentRun(
        id=new_id(IdKind.AGENT_RUN),
        job_id=job.id,
        role=AgentRole.REPOSITORY_ANALYZER,
        status=AgentStatus.COMPLETED,
        prompt_version=result.prompt_version,
        provider=record.provider,
        model=record.model,
        started_at=record.created_at,
        completed_at=completed_at,
        latency_seconds=record.latency_seconds,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        estimated_cost_usd=record.estimated_cost_usd,
        context_digest=_digest(variables),
        error=None,
    )


def _digest(variables: dict[str, str]) -> str:
    """Return a stable digest of what the model was shown.

    Only the digest is stored, never the context: agent context contains repository text, and
    a table of it would be both enormous and a new place secrets could land.
    """
    payload = json.dumps(variables, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _prompt_variables(*, summary: RepositorySummary, change_set: ChangeSet) -> dict[str, str]:
    """Build the variables a repository-analyzer prompt may reference."""
    return {
        "repository_full_name": summary.repository.full_name,
        "commit_sha": summary.commit_sha,
        "file_count": str(summary.file_count),
        "languages": ", ".join(summary.stack.languages),
        "frameworks": ", ".join(summary.stack.frameworks),
        "test_frameworks": ", ".join(summary.stack.test_frameworks),
        "services": ", ".join(service.path for service in summary.services),
        "changed_paths": "\n".join(change_set.changed_paths),
        "changed_file_count": str(len(change_set.files)),
        "entry_points": ", ".join(summary.entry_points),
        "api_surfaces": ", ".join(summary.api_surfaces),
        "notes": "\n".join(summary.notes),
        "deterministic_summary": json.dumps(summary.model_dump(mode="json"), sort_keys=True),
    }


def deterministic_narrative(
    *,
    summary: RepositorySummary,
    change_set: ChangeSet,
) -> str:
    """Render what Scout actually determined, without adding anything it did not.

    This is the floor: it is always available, always identical for the same commit, and it
    never claims an architectural judgement.
    """
    stack = summary.stack
    lines = [
        (
            f"{summary.repository.full_name} at {summary.commit_sha[:12]} contains "
            f"{summary.file_count} tracked file(s)."
        ),
        f"Languages: {', '.join(stack.languages) or 'none detected'}.",
        f"Frameworks: {', '.join(stack.frameworks) or 'none detected'}.",
        f"Test frameworks: {', '.join(stack.test_frameworks) or 'none detected'}.",
        f"Package managers: {', '.join(stack.package_managers) or 'none detected'}.",
        f"CI systems: {', '.join(stack.ci_systems) or 'none detected'}.",
        "Deployment signals: "
        + ("containerised" if stack.containerized else "no container configuration detected")
        + (
            "; infrastructure-as-code present"
            if stack.has_infrastructure_as_code
            else "; no infrastructure-as-code detected"
        )
        + ".",
        f"Service roots: {', '.join(service.path for service in summary.services) or '.'}.",
        f"Test roots: {', '.join(summary.test_paths) or 'none detected'}.",
        f"Documentation roots: {', '.join(summary.documentation_paths) or 'none detected'}.",
        f"Migration roots: {', '.join(summary.migration_paths) or 'none detected'}.",
        "This change touches "
        f"{len(change_set.files)} file(s): {', '.join(change_set.changed_paths[:20])}"
        + (" and more." if len(change_set.changed_paths) > 20 else "."),
    ]
    if summary.entry_points:
        lines.append(f"Candidate entry points: {', '.join(summary.entry_points[:10])}.")
    if summary.api_surfaces:
        lines.append(f"API surfaces: {', '.join(summary.api_surfaces[:10])}.")
    if summary.auth_mechanisms:
        lines.append(
            "Authentication or authorization code appears at: "
            f"{', '.join(summary.auth_mechanisms[:10])}."
        )
    if summary.notes:
        lines.append("Notes: " + "; ".join(summary.notes) + ".")
    return "\n".join(lines)[:_MAX_NARRATIVE]
