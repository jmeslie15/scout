"""FEATURE_VERIFYING: did the change do what was asked? (spec sections 23-25)

Spec section 23 draws the distinction this stage exists to preserve: "the tests passed" is not
the same claim as "the requested feature works". So every acceptance criterion becomes a
:class:`~scout_domain.models.report.CriterionVerification`, and a criterion is only ``PASSED``
when a concrete procedure was actually executed and its observed result recorded. The domain
model enforces that a ``PASSED`` criterion carries evidence; this stage's job is to have
evidence to carry.

Requirement material is taken in the order the domain declares, because the earlier source is
the more authoritative claim about intent:

1. ``feature_verification.acceptance_criteria`` from ``.scout/engineering.yaml`` -- explicit,
   written down, and the repository's own words.
2. The pull-request description and commit messages, via
   :func:`scout_github.extract_requirement_text`.
3. The tests this change added or modified -- the weakest source, used only when nothing
   states the intent, and recorded as such on every criterion it produces.

A criterion with no executable procedure is ``NOT_VERIFIED`` (or ``BLOCKED`` when the test
runtime itself produced no result). It is never ``PASSED``, because passing something Scout did
not run would be the exact overstatement spec section 2 forbids.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from scout_domain.enums import CriterionStatus, EvidenceKind, JobStatus, ToolStatus
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.report import CriterionVerification, ReportSection, ReportSectionStatus
from scout_observability import get_logger
from scout_orchestrator.stages.context import BaseStage, StageContext, StageResult
from scout_orchestrator.stages.tool_stage import TEST_TOOL_NAMES
from scout_tools.detection import classify_paths

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scout_domain.config import ScoutRepositoryConfig
    from scout_domain.models.artifact import Artifact
    from scout_domain.models.tool import ToolResult
    from scout_tools.artifacts import ArtifactStore

__all__ = [
    "FeatureVerificationStage",
    "build_criteria_for",
    "criteria_section",
    "needs_unverified_section",
    "passing_tests_from_artifacts",
    "test_runtime_available",
]

_log = get_logger(__name__)

_STAGE: Final = JobStatus.FEATURE_VERIFYING

#: Minimum characters and words for a line of prose to be treated as an acceptance criterion.
#: Below both thresholds a fragment ("add it") is noise rather than a requirement.
_MIN_CRITERION_CHARS: Final = 15
_MIN_CRITERION_WORDS: Final = 3

#: Ceiling on criteria derived from one requirement document, so a verbose description does
#: not turn the report into a checklist.
_MAX_CRITERIA: Final = 12

#: Tokens that appear in almost every requirement sentence and therefore carry no signal when
#: matching a criterion to a test.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "about",
        "added",
        "adding",
        "adds",
        "after",
        "again",
        "against",
        "allow",
        "allows",
        "also",
        "before",
        "being",
        "between",
        "both",
        "cannot",
        "change",
        "changed",
        "changes",
        "could",
        "does",
        "each",
        "either",
        "ensure",
        "ensures",
        "from",
        "have",
        "helper",
        "into",
        "make",
        "makes",
        "more",
        "most",
        "must",
        "need",
        "needs",
        "only",
        "other",
        "over",
        "same",
        "should",
        "some",
        "such",
        "support",
        "supports",
        "test",
        "tests",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "under",
        "update",
        "updated",
        "updates",
        "used",
        "uses",
        "using",
        "very",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "without",
        "would",
        "your",
    }
)

#: Requirement headings produced by :func:`scout_github.extract_requirement_text`, mapped to
#: the ``source`` recorded on each criterion so a reader can see where it came from.
_SOURCE_BY_HEADING: Final[dict[str, str]] = {
    "## Pull request description": "pull_request_body",
    "## Linked issue": "linked_issue",
    "## Commit messages": "commit_messages",
}

#: Tokens are split on underscores as well as non-alphanumerics, so a criterion that says
#: "multiply" matches a test named ``test_multiply``. Keeping ``_`` inside a token would make the
#: test name a single opaque word and the match would never fire.
_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")


class FeatureVerificationStage(BaseStage):
    """Turn acceptance criteria into verified, unverified, or blocked statements."""

    stage = _STAGE

    async def run(self, ctx: StageContext) -> StageResult:
        """Build one verification entry per acceptance criterion."""
        config = ctx.require_repository_config()
        test_results = tuple(
            result for result in ctx.inputs.tool_results if result.tool in TEST_TOOL_NAMES
        )
        passing_tests = passing_tests_from_artifacts(ctx.inputs.artifacts, ctx.artifacts)
        runtime_available = test_runtime_available(test_results, passing_tests)

        criteria = build_criteria_for(
            config=config,
            requirement_text=ctx.inputs.requirement_text,
            changed_paths=ctx.changed_paths,
            passing_tests=passing_tests,
            test_results=test_results,
            test_runtime_available=runtime_available,
            head_sha=ctx.job.head_sha,
        )

        return StageResult.succeeded_for(
            self.stage,
            narrative=_narrative(criteria),
            criteria=criteria,
            sections=(
                () if not needs_unverified_section(criteria) else (criteria_section(criteria),)
            ),
            payload={
                "criteria_total": len(criteria),
                "criteria_passed": sum(
                    1 for criterion in criteria if criterion.status is CriterionStatus.PASSED
                ),
                "criteria_not_verified": [
                    criterion.criterion
                    for criterion in criteria
                    if criterion.status is not CriterionStatus.PASSED
                ],
                "test_runtime_available": runtime_available,
            },
        )


# --------------------------------------------------------------------------------------
# Criteria construction
# --------------------------------------------------------------------------------------


def build_criteria_for(
    *,
    config: ScoutRepositoryConfig,
    requirement_text: str,
    changed_paths: Sequence[str],
    passing_tests: Sequence[str],
    test_results: Sequence[ToolResult],
    test_runtime_available: bool,
    head_sha: str,
) -> tuple[CriterionVerification, ...]:
    """Build the criterion verifications from the highest-authority source available.

    A repository that has switched feature verification off gets no criteria at all, and therefore
    no unverified area: Scout states nothing rather than asserting something it did not check. The
    stage is skipped with a reason by the orchestrator, and the report lists the opt-out as a
    limitation.
    """
    if not config.feature_verification.enabled:
        return ()

    declared = [text for text in config.feature_verification.acceptance_criteria if text.strip()]
    if declared:
        return tuple(
            _verify(
                text,
                source="acceptance_criteria",
                passing_tests=passing_tests,
                test_results=test_results,
                test_runtime_available=test_runtime_available,
            )
            for text in declared[:_MAX_CRITERIA]
        )

    extracted = _criteria_from_requirement_text(requirement_text)
    if extracted:
        return tuple(
            _verify(
                text,
                source=source,
                passing_tests=passing_tests,
                test_results=test_results,
                test_runtime_available=test_runtime_available,
            )
            for text, source in extracted[:_MAX_CRITERIA]
        )

    return _criteria_from_changed_tests(
        changed_paths=changed_paths,
        test_results=test_results,
        test_runtime_available=test_runtime_available,
        head_sha=head_sha,
    )


def _criteria_from_requirement_text(text: str) -> list[tuple[str, str]]:
    """Extract candidate criteria from requirement material, preserving provenance."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    source = "pull_request_body"

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            for heading, mapped in _SOURCE_BY_HEADING.items():
                if line.startswith(heading):
                    source = mapped
                    break
            continue

        cleaned = line.lstrip("-*").strip()
        normalised = " ".join(cleaned.split())
        if not _is_criterion_like(normalised):
            continue
        fingerprint = normalised.lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append((normalised, source))
        if len(candidates) >= _MAX_CRITERIA:
            break
    return candidates


def _is_criterion_like(line: str) -> bool:
    """Whether a line reads like an acceptance criterion rather than a fragment."""
    if len(line) < _MIN_CRITERION_CHARS:
        return False
    return len(line.split()) >= _MIN_CRITERION_WORDS


def _criteria_from_changed_tests(
    *,
    changed_paths: Sequence[str],
    test_results: Sequence[ToolResult],
    test_runtime_available: bool,
    head_sha: str,
) -> tuple[CriterionVerification, ...]:
    """Fall back to the test files this change added or modified.

    This is the weakest source, and it is treated that way: a criterion is only ``PASSED`` when
    the test run actually executed and reported a result, and every criterion says in its notes
    that it came from the suite rather than from a stated requirement.
    """
    test_paths = classify_paths(changed_paths)["tests"]
    if not test_paths:
        return ()
    primary = _primary_test_result(test_results)
    verifications: list[CriterionVerification] = []
    for path in test_paths[:5]:
        status = (
            CriterionStatus.PASSED
            if test_runtime_available
            and primary is not None
            and primary.status is ToolStatus.PASSED
            else CriterionStatus.BLOCKED
        )
        verifications.append(
            CriterionVerification(
                criterion=f"the tests in {path} pass at the verified commit",
                status=status,
                procedure=[f"run {primary.tool if primary else 'the test runner'} over {path}"],
                expected_result="every collected test passes",
                actual_result=_actual_result(primary, head_sha=head_sha)
                if status is CriterionStatus.PASSED
                else "",
                evidence=_test_evidence(primary) if status is CriterionStatus.PASSED else [],
                notes=(
                    "derived from the test files this change modified because neither "
                    ".scout/engineering.yaml nor the pull-request description stated an "
                    "acceptance criterion"
                ),
                source="existing_tests",
            )
        )
    return tuple(verifications)


def _verify(
    criterion: str,
    *,
    source: str,
    passing_tests: Sequence[str],
    test_results: Sequence[ToolResult],
    test_runtime_available: bool,
) -> CriterionVerification:
    """Assess one *stated* criterion, which Scout has no procedure for yet.

    A criterion that came from prose -- ``.scout/engineering.yaml`` or the pull-request description
    -- describes an intent, and Scout has not derived a procedure that demonstrates *that intent*.
    A test whose name happens to share a word with it is a candidate, not evidence: treating the
    overlap as a pass would be exactly the "the tests passed, therefore the feature works"
    inference spec section 23 forbids, and would let a criterion pass on the strength of a test
    written for something else.

    So the outcome is ``NOT_VERIFIED`` (or ``BLOCKED`` when the test runtime produced nothing), and
    the overlapping test is recorded as a lead for the human to follow. Deriving and executing a
    procedure per criterion is the feature-verification agent's job (spec section 85, a later
    slice); until it is wired, Scout reports the gap rather than papering over it.
    """
    overlap = _overlapping_passing_test(criterion, passing_tests)
    primary = _primary_test_result(test_results)
    lead = (
        ""
        if overlap is None
        else (
            f" The suite contains {overlap}, whose name overlaps this criterion; Scout has not "
            "established that it exercises the stated intent."
        )
    )

    if not test_runtime_available:
        return CriterionVerification(
            criterion=criterion,
            status=CriterionStatus.BLOCKED,
            procedure=[],
            expected_result="a procedure demonstrating this criterion is executed",
            actual_result="",
            evidence=[],
            notes=(
                "the test runtime produced no result for this revision, so Scout could not attempt "
                "this criterion at all." + lead
            ),
            source=source,
        )

    return CriterionVerification(
        criterion=criterion,
        status=CriterionStatus.NOT_VERIFIED,
        procedure=[
            f"run {primary.tool if primary else 'the test suite'} over the verified checkout",
            (
                "no procedure derived from this criterion was executed, so its intent is "
                "unverified even though the suite ran"
            ),
        ],
        expected_result="execution of a procedure that demonstrates this criterion",
        actual_result="",
        evidence=[],
        notes=(
            "not verified is not a failure: Scout states that it had no procedure for this "
            "criterion, which is why it is surfaced for a human rather than assumed." + lead
        ),
        source=source,
    )


# --------------------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------------------


def passing_tests_from_artifacts(
    artifacts: Sequence[Artifact],
    store: ArtifactStore,
) -> tuple[str, ...]:
    """Return the node ids of tests that passed at the verified commit.

    Read from the JUnit report the test runner wrote, because the runner's console summary names
    only the failures. When the report is missing or unreadable this returns an empty tuple: an
    unreadable report must not be mistaken for "everything passed".

    Takes the artifact locators and a store rather than a stage context so the orchestrator can
    rebuild criteria from persisted state when it builds the report after a resume.
    """
    report = next(
        (artifact for artifact in artifacts if artifact.kind is ArtifactKind.TEST_REPORT), None
    )
    if report is None:
        return ()
    try:
        payload = store.get_bytes(report)
    except Exception as exc:
        _log.warning(
            "could not read the JUnit report; criteria cannot cite individual tests",
            error=f"{type(exc).__name__}: {exc}",
        )
        return ()
    return _parse_passing_tests(payload)


def _parse_passing_tests(payload: bytes) -> tuple[str, ...]:
    """Extract node ids that passed from a JUnit XML report."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(payload.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return ()

    passing: list[str] = []
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            continue
        if case.find("skipped") is not None:
            continue
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        if not name:
            continue
        node = f"{classname.replace('.', '/')}::{name}" if classname else name
        passing.append(node)
    return tuple(passing)


def _overlapping_passing_test(criterion: str, passing_tests: Sequence[str]) -> str | None:
    """Return the passing test whose name shares a significant token with ``criterion``.

    A *lead*, not evidence: the caller records it for a human and never promotes it to a pass.

    Deterministic and explainable: the longest shared significant token wins, so "multiply"
    matches ``test_multiply`` and a generic word like "helper" (a stopword) never matches
    anything. Ties are broken by the test's own order, so two runs agree.
    """
    criterion_tokens = _significant_tokens(criterion)
    if not criterion_tokens or not passing_tests:
        return None

    best: tuple[int, int, str] | None = None
    for node in passing_tests:
        node_tokens = _significant_tokens(node)
        shared = criterion_tokens & node_tokens
        if not shared:
            continue
        longest = max(len(token) for token in shared)
        candidate = (len(shared), longest, node)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return None if best is None else best[2]


def _significant_tokens(text: str) -> set[str]:
    """Tokenise text, dropping stopwords and short fragments."""
    tokens: set[str] = set()
    for token in _TOKEN_PATTERN.findall(text.lower()):
        if len(token) < 4 or token in _STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def test_runtime_available(
    test_results: Sequence[ToolResult], passing_tests: Sequence[str]
) -> bool:
    """Whether the test runtime produced a usable result for this revision.

    A test run that *failed* is available: the runtime worked, the tests failed, and that is
    information a criterion can rest on. Only a run that never happened (or that failed for
    infrastructure reasons) makes every criterion unverifiable.
    """
    if passing_tests:
        return True
    return any(
        not result.infrastructure_failure and result.exit_code is not None
        for result in test_results
    )


def _test_evidence(result: ToolResult | None) -> list[EvidenceReference]:
    """Build the machine evidence a passed criterion cites."""
    if result is None:
        return []
    return [
        EvidenceReference(
            kind=EvidenceKind.TEST_RUN,
            reference_id=result.id,
            summary=f"{result.tool} executed the suite for this revision",
            artifact_id=result.report_artifact_id or result.stdout_artifact_id,
        )
    ]


def _primary_test_result(results: Sequence[ToolResult]) -> ToolResult | None:
    """Return the test result a criterion should cite, preferring pytest."""
    return next((result for result in results if result.tool == "pytest"), None) or (
        results[0] if results else None
    )


def _actual_result(result: ToolResult | None, *, head_sha: str) -> str:
    """Render the observed test outcome for a passed criterion."""
    if result is None:
        return ""
    passed = result.summary.get_int("passed")
    total = result.summary.get_int("total")
    return f"{result.tool} reported {passed} passed of {total} collected at {head_sha[:12]}"


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def needs_unverified_section(criteria: Sequence[CriterionVerification]) -> bool:
    """Whether any criterion failed to verify, which the report must state explicitly."""
    return any(criterion.status is not CriterionStatus.PASSED for criterion in criteria)


def criteria_section(criteria: Sequence[CriterionVerification]) -> ReportSection:
    """Render the unverified criteria as an ``UNVERIFIED`` report section."""
    unresolved = [
        criterion for criterion in criteria if criterion.status is not CriterionStatus.PASSED
    ]
    reason = (
        f"{len(unresolved)} of {len(criteria)} acceptance criterion/criteria could not be "
        "verified by executing a procedure"
    )
    return ReportSection(
        key="feature_verification.criteria",
        title="Feature verification",
        status=ReportSectionStatus.UNVERIFIED,
        summary=reason,
        details=[
            f"[{criterion.status.value}] {criterion.criterion}"
            + (f" -- {criterion.notes}" if criterion.notes else "")
            for criterion in unresolved[:20]
        ],
        unverified_reason=reason,
    )


def _narrative(criteria: Sequence[CriterionVerification]) -> str:
    """Summarise the criterion results."""
    if not criteria:
        return "no acceptance criterion was available to verify"
    passed = sum(1 for criterion in criteria if criterion.status is CriterionStatus.PASSED)
    return f"verified {passed} of {len(criteria)} acceptance criterion/criteria by execution"
