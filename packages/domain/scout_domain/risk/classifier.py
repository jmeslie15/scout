"""Risk-based execution depth (spec sections 41 and 77).

Not every pull request deserves the same treatment. A README typo and a change to the
authorization layer must not consume the same compute or require the same human gate.

Classification is **deterministic and explainable**: it is driven by path patterns, file
lifecycle, and optional pre-computed content signals, and every assessment carries the
list of signals that produced it. An LLM never decides risk, because risk decides how
much verification a change receives and how much human authority it needs.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from scout_domain.enums import RiskLevel
from scout_domain.models.repository import ChangeSet, ChangeType

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "DEFAULT_RISK_RULES",
    "ApprovalGate",
    "RiskAssessment",
    "RiskRule",
    "RiskSignal",
    "classify_change_set",
    "classify_paths",
    "gate_for_risk",
]


@dataclass(frozen=True, slots=True)
class RiskRule:
    """A path-pattern rule that raises a change's risk floor."""

    name: str
    risk: RiskLevel
    reason: str
    patterns: tuple[str, ...]

    def matches(self, path: str) -> bool:
        """Glob-match ``path`` against this rule's patterns.

        Two subtleties, both of which caused real misclassification before they were
        handled:

        * Patterns are matched against the full repository-relative path *and* against
          every suffix of it, so ``migrations/**`` matches ``apps/api/migrations/001.py``
          as well as a top-level ``migrations/`` directory.
        * A leading ``**/`` is also tried without the prefix. ``fnmatch`` has no special
          handling for ``**`` and treats it as a single-segment ``*``, so ``**/*.md`` would
          otherwise fail to match a top-level ``README.md``.
        """
        parts = path.split("/")
        candidates = [path, *("/".join(parts[index:]) for index in range(1, len(parts)))]

        expanded: list[str] = []
        for pattern in self.patterns:
            expanded.append(pattern)
            if pattern.startswith("**/"):
                expanded.append(pattern[3:])

        return any(
            fnmatch.fnmatchcase(candidate, pattern)
            for candidate in candidates
            for pattern in expanded
        )


#: The rules, ordered most severe first. Ordering only affects presentation; the
#: assessment takes the maximum risk across all matching rules.
DEFAULT_RISK_RULES: Final[tuple[RiskRule, ...]] = (
    # -- CRITICAL: security boundaries, credential systems, production config ---------
    RiskRule(
        name="credential_system",
        risk=RiskLevel.CRITICAL,
        reason="change touches credential or secret handling",
        patterns=(
            "**/*credential*",
            "**/*secret*",
            "**/*vault*",
            "**/*.pem",
            "**/*.p8",
            "**/*.key",
            "**/kms/**",
            "**/iam/**",
        ),
    ),
    RiskRule(
        name="security_boundary",
        risk=RiskLevel.CRITICAL,
        reason="change touches a security boundary",
        patterns=(
            "**/security/**",
            "**/crypto/**",
            "**/tls/**",
            "**/csrf/**",
            "**/cors/**",
            "**/sandbox/**/policy*",
        ),
    ),
    RiskRule(
        name="production_configuration",
        risk=RiskLevel.CRITICAL,
        reason="change touches production configuration or deployment policy",
        patterns=(
            "**/prod/**",
            "**/production/**",
            "**/values-prod*.y*ml",
            "**/*.production.y*ml",
            "**/prod.*.y*ml",
        ),
    ),
    # -- HIGH: authn/authz, migrations, money, user data, infrastructure --------------
    RiskRule(
        name="authentication",
        risk=RiskLevel.HIGH,
        reason="change touches authentication or authorization",
        patterns=(
            "**/auth/**",
            "**/*auth*",
            "**/permissions/**",
            "**/*permission*",
            "**/*authoriz*",
            "**/rbac/**",
            "**/acl/**",
            "**/login*",
            "**/session*",
            "**/oauth*",
            "**/jwt*",
        ),
    ),
    RiskRule(
        name="database_migration",
        risk=RiskLevel.HIGH,
        reason="change includes a database migration",
        patterns=(
            "**/migrations/**",
            "migrations/**",
            "**/alembic/versions/**",
            "**/flyway/**",
            "**/liquibase/**",
            "**/*.sql",
        ),
    ),
    RiskRule(
        name="payment_logic",
        risk=RiskLevel.HIGH,
        reason="change touches payment or billing logic",
        patterns=(
            "**/*payment*",
            "**/*billing*",
            "**/*invoice*",
            "**/*stripe*",
            "**/*checkout*",
            "**/*subscription*",
        ),
    ),
    RiskRule(
        name="user_data",
        risk=RiskLevel.HIGH,
        reason="change touches user data models or privacy handling",
        patterns=(
            "**/*pii*",
            "**/gdpr/**",
            "**/*personal_data*",
            "**/models/user*",
            "**/models/account*",
            "**/models/profile*",
        ),
    ),
    RiskRule(
        name="infrastructure",
        risk=RiskLevel.HIGH,
        reason="change touches infrastructure, containers, or CI/CD",
        patterns=(
            "**/infra/**",
            "**/terraform/**",
            "**/*.tf",
            "**/*.tfvars",
            "**/k8s/**",
            "**/kubernetes/**",
            "**/helm/**",
            "**/charts/**",
            "Dockerfile*",
            "**/Dockerfile*",
            "docker-compose*.y*ml",
            ".github/workflows/**",
            ".github/actions/**",
            "**/*.dockerfile",
        ),
    ),
    # -- MEDIUM: application code, dependencies, configuration -----------------------
    RiskRule(
        name="dependency_manifest",
        risk=RiskLevel.MEDIUM,
        reason="change modifies the dependency graph",
        patterns=(
            "pyproject.toml",
            "**/pyproject.toml",
            "requirements*.txt",
            "**/requirements*.txt",
            "poetry.lock",
            "uv.lock",
            "package.json",
            "**/package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "go.mod",
            "go.sum",
            "Cargo.toml",
            "Cargo.lock",
            "Gemfile*",
        ),
    ),
    RiskRule(
        name="application_code",
        risk=RiskLevel.MEDIUM,
        reason="change modifies application source",
        patterns=(
            "**/*.py",
            "**/*.ts",
            "**/*.tsx",
            "**/*.js",
            "**/*.jsx",
            "**/*.mjs",
            "**/*.cjs",
            "**/*.go",
            "**/*.rs",
            "**/*.java",
            "**/*.kt",
            "**/*.rb",
            "**/*.php",
            "**/*.cs",
            "**/*.swift",
            "**/*.c",
            "**/*.cc",
            "**/*.cpp",
            "**/*.h",
            "**/*.hpp",
            "**/*.scala",
            "**/*.sh",
            "**/*.bash",
        ),
    ),
    RiskRule(
        name="api_surface",
        risk=RiskLevel.MEDIUM,
        reason="change touches an API surface consumed by others",
        patterns=(
            "**/api/**",
            "**/routes/**",
            "**/routers/**",
            "**/controllers/**",
            "**/endpoints/**",
            "**/handlers/**",
            "**/graphql/**",
            "**/proto/**",
            "**/openapi*.y*ml",
            "**/openapi*.json",
        ),
    ),
    RiskRule(
        name="ui_change",
        risk=RiskLevel.MEDIUM,
        reason="change touches the user interface",
        patterns=(
            "**/components/**",
            "**/pages/**",
            "**/views/**",
            "**/screens/**",
            "**/ui/**",
            "**/*.css",
            "**/*.scss",
        ),
    ),
    RiskRule(
        name="runtime_configuration",
        risk=RiskLevel.MEDIUM,
        reason="change modifies runtime configuration",
        patterns=(
            "**/*.yaml",
            "**/*.yml",
            "**/*.toml",
            "**/*.ini",
            "**/*.cfg",
            "**/*.env",
            "**/*.json",
        ),
    ),
    # -- LOW: documentation, formatting, non-functional -----------------------------
    RiskRule(
        name="documentation",
        risk=RiskLevel.LOW,
        reason="change is documentation only",
        patterns=(
            "**/*.md",
            "**/*.rst",
            "**/*.txt",
            "**/*.adoc",
            "docs/**",
            "**/docs/**",
            "LICENSE*",
            "**/LICENSE*",
            "CHANGELOG*",
            "**/CHANGELOG*",
            "CODEOWNERS",
            ".gitignore",
            ".editorconfig",
            ".gitattributes",
        ),
    ),
    RiskRule(
        name="asset",
        risk=RiskLevel.LOW,
        reason="change is a non-executable asset",
        patterns=(
            "**/*.png",
            "**/*.jpg",
            "**/*.jpeg",
            "**/*.gif",
            "**/*.svg",
            "**/*.ico",
            "**/*.webp",
            "**/*.woff",
            "**/*.woff2",
        ),
    ),
)

#: Signals that are *decisive*: they describe the entire diff, so they override any
#: path-based rule. A whitespace-only change to ``auth/login.py`` genuinely is low risk,
#: and reporting it as HIGH would train reviewers to ignore Scout.
DECISIVE_SIGNALS: Final[frozenset[str]] = frozenset({"formatting_only", "comments_only"})

#: Churn above which a change is escalated to at least MEDIUM regardless of paths.
#: A very large diff is harder to reason about and deserves a real review.
LARGE_CHANGE_LINES: Final = 2_000
LARGE_CHANGE_FILES: Final = 60


@dataclass(frozen=True, slots=True)
class RiskSignal:
    """One reason contributing to a risk assessment."""

    rule: str
    risk: RiskLevel
    reason: str
    path: str | None = None

    def render(self) -> str:
        location = f" ({self.path})" if self.path else ""
        return f"[{self.risk.value}] {self.reason}{location}"


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """A risk level plus the reasoning that produced it.

    The rationale is not decoration: it is surfaced in the verification report so a
    reviewer can tell *why* a change was treated as high risk, and dispute it.
    """

    level: RiskLevel
    signals: tuple[RiskSignal, ...] = field(default_factory=tuple)
    default_risk: RiskLevel = RiskLevel.MEDIUM

    @property
    def matched_rules(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for signal in self.signals:
            seen.setdefault(signal.rule, None)
        return tuple(seen)

    @property
    def is_elevated(self) -> bool:
        """Whether any signal raised the level above the repository default."""
        return self.level is not self.default_risk

    def explain(self) -> str:
        """Render a human-readable explanation for the report."""
        if not self.signals:
            return (
                f"Classified {self.level.value} using the repository default "
                f"({self.default_risk.value}); no risk signal matched."
            )
        lines = [f"Classified {self.level.value} because:"]
        lines.extend(f"- {signal.render()}" for signal in self.signals)
        return "\n".join(lines)


def _assess(
    *,
    paths: Iterable[tuple[str, ChangeType]],
    default_risk: RiskLevel,
    signals: Mapping[str, bool] | None,
    total_additions: int = 0,
    total_deletions: int = 0,
    file_count: int = 0,
    rules: tuple[RiskRule, ...] = DEFAULT_RISK_RULES,
) -> RiskAssessment:
    # Track only the strongest signal per rule so a 40-file docs change does not emit
    # 40 identical lines into the report.
    best_per_rule: dict[str, RiskSignal] = {}

    for path, change_type in paths:
        for rule in rules:
            if not rule.matches(path):
                continue
            existing = best_per_rule.get(rule.name)
            if existing is None or rule.risk.depth_rank > existing.risk.depth_rank:
                best_per_rule[rule.name] = RiskSignal(
                    rule=rule.name,
                    risk=rule.risk,
                    reason=rule.reason,
                    path=path,
                )
        if change_type is ChangeType.DELETED:
            key = "file_deleted"
            if key not in best_per_rule:
                best_per_rule[key] = RiskSignal(
                    rule=key,
                    risk=RiskLevel.MEDIUM,
                    reason="change deletes a file",
                    path=path,
                )

    # Content signals supplied by the repository analyzer. These exist so that a
    # destructive migration or a formatting-only diff can be recognised without Scout
    # having to parse every language itself.
    for key, reason, risk in _CONTENT_SIGNALS:
        if signals and signals.get(key):
            best_per_rule.setdefault(key, RiskSignal(rule=key, risk=risk, reason=reason, path=None))

    if total_additions + total_deletions > LARGE_CHANGE_LINES or file_count > LARGE_CHANGE_FILES:
        best_per_rule.setdefault(
            "large_change",
            RiskSignal(
                rule="large_change",
                risk=RiskLevel.MEDIUM,
                reason=(
                    f"large change: {file_count} file(s), "
                    f"{total_additions + total_deletions} changed line(s)"
                ),
                path=None,
            ),
        )

    found = list(best_per_rule.values())

    # An explicit low-risk determination (formatting- or comment-only) describes the whole
    # diff, so it wins outright. Other signals are still reported for transparency.
    decisive = [signal for signal in found if signal.rule in DECISIVE_SIGNALS]
    if decisive:
        level = RiskLevel.LOW
    elif found:
        # A matched rule is a positive determination about this change, so it decides the
        # level. `default_risk` is the answer only when nothing matched.
        level = max((signal.risk for signal in found), key=lambda risk: risk.depth_rank)
    else:
        level = default_risk

    ordered = tuple(sorted(found, key=lambda s: (-s.risk.depth_rank, s.rule)))
    return RiskAssessment(level=level, signals=ordered, default_risk=default_risk)


#: Optional boolean signals a caller may supply, with the risk they imply.
_CONTENT_SIGNALS: Final[tuple[tuple[str, str, RiskLevel], ...]] = (
    (
        "destructive_migration",
        "migration drops or rewrites data",
        RiskLevel.CRITICAL,
    ),
    (
        "real_money_path",
        "change affects real payment or refund flow",
        RiskLevel.CRITICAL,
    ),
    (
        "changes_public_api_contract",
        "change alters a published API contract",
        RiskLevel.HIGH,
    ),
    (
        "formatting_only",
        "change is formatting or whitespace only",
        RiskLevel.LOW,
    ),
    (
        "comments_only",
        "change only alters comments",
        RiskLevel.LOW,
    ),
)


def classify_change_set(
    change_set: ChangeSet,
    *,
    default_risk: RiskLevel = RiskLevel.MEDIUM,
    signals: Mapping[str, bool] | None = None,
) -> RiskAssessment:
    """Classify a change set (spec section 41)."""
    return _assess(
        paths=((file.path, file.change_type) for file in change_set.files),
        default_risk=default_risk,
        signals=signals,
        total_additions=change_set.total_additions,
        total_deletions=change_set.total_deletions,
        file_count=len(change_set.files),
    )


def classify_paths(
    paths: Iterable[str],
    *,
    default_risk: RiskLevel = RiskLevel.MEDIUM,
    signals: Mapping[str, bool] | None = None,
) -> RiskAssessment:
    """Classify a bare list of changed paths.

    Used by the webhook handler to pick a scheduling priority before the diff has been
    fetched, so that a security-relevant pull request is not stuck behind a docs change.
    """
    materialised = [(path, ChangeType.MODIFIED) for path in paths]
    return _assess(
        paths=materialised,
        default_risk=default_risk,
        signals=signals,
    )


@dataclass(frozen=True, slots=True)
class ApprovalGate:
    """What a change at this risk level is allowed to do (spec section 77).

    This encodes the *planned* controlled-enforcement policy. In the initial
    observe-only pilot every gate is advisory; the model exists now so that enforcement
    is a configuration change rather than a redesign.
    """

    risk: RiskLevel
    automated_verification: bool
    requires_human_review: bool
    requires_devops_approval: bool
    requires_senior_approval: bool
    #: False during the observe-only pilot: Scout reports but never blocks.
    blocking: bool = False

    @property
    def required_approvals(self) -> int:
        return int(self.requires_devops_approval) + int(self.requires_senior_approval)


_GATES: Final[dict[RiskLevel, ApprovalGate]] = {
    RiskLevel.LOW: ApprovalGate(
        risk=RiskLevel.LOW,
        automated_verification=True,
        requires_human_review=False,
        requires_devops_approval=False,
        requires_senior_approval=False,
    ),
    RiskLevel.MEDIUM: ApprovalGate(
        risk=RiskLevel.MEDIUM,
        automated_verification=True,
        requires_human_review=True,
        requires_devops_approval=False,
        requires_senior_approval=False,
    ),
    RiskLevel.HIGH: ApprovalGate(
        risk=RiskLevel.HIGH,
        automated_verification=True,
        requires_human_review=True,
        requires_devops_approval=True,
        requires_senior_approval=False,
    ),
    RiskLevel.CRITICAL: ApprovalGate(
        risk=RiskLevel.CRITICAL,
        automated_verification=True,
        requires_human_review=True,
        requires_devops_approval=True,
        requires_senior_approval=True,
    ),
}


def gate_for_risk(risk: RiskLevel, *, blocking: bool = False) -> ApprovalGate:
    """Return the approval gate policy for ``risk``.

    Args:
        blocking: Set ``True`` only after the observe-only pilot, when Scout is
            authorised to influence development gates.
    """
    gate = _GATES[risk]
    return (
        gate
        if gate.blocking == blocking
        else ApprovalGate(
            risk=gate.risk,
            automated_verification=gate.automated_verification,
            requires_human_review=gate.requires_human_review,
            requires_devops_approval=gate.requires_devops_approval,
            requires_senior_approval=gate.requires_senior_approval,
            blocking=blocking,
        )
    )
