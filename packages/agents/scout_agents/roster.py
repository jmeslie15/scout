"""The canonical agent roster: who exists, what each is for, and what each may do.

Spec section 40 fixes the set of reasoning agents. Agents do **not** spawn other agents; the
orchestrator schedules each role explicitly. That makes the roster a build-time constant rather
than something discovered at runtime, so it is declared once here and read by everything that
needs it — the dashboard's agent page, the capability check before an agent runs, and the
documentation.

Least privilege (spec section 54) is the reason this table exists at all. An agent receives
only the capabilities its role requires, and the check happens **outside** the agent, so a
hallucinating or prompt-injected agent cannot widen its own reach. Note what the table says:

* The report generator can search knowledge and nothing else. It renders what other stages
  produced; it cannot run a tool or reach GitHub.
* The security agent can scan but cannot write a check run, so a security finding cannot
  become a GitHub status without passing through the orchestrator.
* Only the review and investigation roles may read arbitrary repository files and ask for a
  diff, because those are the roles whose job is to understand code in context.
* No role has ``github.write.check``. Publishing a check run is an orchestrator action driven
  by the derived recommendation, deliberately not something an agent can trigger directly
  (spec section 3.1: an LLM does not decide a deterministic outcome).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from scout_domain.enums import AgentRole, Capability

__all__ = ["AGENT_ROSTER", "AgentSpec", "capabilities_for", "describe"]


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """What one agent role is, does, and is permitted to do."""

    role: AgentRole
    description: str
    capabilities: frozenset[Capability]
    #: True once the role's stage is wired into the orchestrator. False means the prompt and
    #: contract exist but nothing schedules it yet, and the dashboard should say so rather
    #: than implying the role is live.
    wired: bool = False


_REPOSITORY_READING: Final[frozenset[Capability]] = frozenset(
    {Capability.REPOSITORY_READ, Capability.REPOSITORY_SEARCH, Capability.GIT_DIFF}
)

AGENT_ROSTER: Final[tuple[AgentSpec, ...]] = (
    AgentSpec(
        role=AgentRole.REPOSITORY_ANALYZER,
        description=(
            "Builds a machine-readable understanding of the repository: services, entry points, "
            "API surfaces, and architecture boundaries. Runs before any change analysis."
        ),
        capabilities=_REPOSITORY_READING,
        # The orchestrator calls the gateway directly for this role today rather than routing
        # through an AgentContext/AgentResult pair; see the deviation note in this package's
        # __init__.
        wired=False,
    ),
    AgentSpec(
        role=AgentRole.CODE_REVIEWER,
        description=(
            "Reviews the change for correctness, maintainability, API behaviour, data handling, "
            "concurrency, error handling, and test quality."
        ),
        capabilities=_REPOSITORY_READING | {Capability.KNOWLEDGE_SEARCH},
    ),
    AgentSpec(
        role=AgentRole.SECURITY,
        description=(
            "Interprets deterministic security scanner output in context and inspects "
            "authentication, authorization, input validation, trust boundaries, and secret "
            "handling. Cannot publish a check run; findings flow through the orchestrator."
        ),
        capabilities=frozenset(
            {
                Capability.REPOSITORY_READ,
                Capability.REPOSITORY_SEARCH,
                Capability.SECURITY_SCAN,
                Capability.DEPENDENCY_SCAN,
                Capability.KNOWLEDGE_SEARCH,
            }
        ),
    ),
    AgentSpec(
        role=AgentRole.TEST_ANALYSIS,
        description=(
            "Distinguishes 'the tests passed' from 'the tests cover the change', and identifies "
            "changed behaviour that no test exercises."
        ),
        capabilities=frozenset(
            {
                Capability.REPOSITORY_READ,
                Capability.TEST_RUN,
                Capability.TEST_GENERATE,
                Capability.KNOWLEDGE_SEARCH,
            }
        ),
    ),
    AgentSpec(
        role=AgentRole.FEATURE_VERIFICATION,
        description=(
            "Extracts acceptance criteria and produces an executable verification plan with "
            "recorded evidence for each criterion."
        ),
        capabilities=frozenset(
            {
                Capability.REPOSITORY_READ,
                Capability.TEST_RUN,
                Capability.TEST_GENERATE,
                Capability.KNOWLEDGE_SEARCH,
            }
        ),
    ),
    AgentSpec(
        role=AgentRole.INVESTIGATION,
        description=(
            "Resolves uncertainty in the documented order: deterministic evidence, repository "
            "inspection, existing tests, documentation, engineering memory, reasoning, and only "
            "then a human question."
        ),
        capabilities=_REPOSITORY_READING | {Capability.KNOWLEDGE_SEARCH},
    ),
    AgentSpec(
        role=AgentRole.REPORT_GENERATOR,
        description=(
            "Aggregates stage results into the structured verification report. It renders what "
            "was observed; it does not decide the recommendation. Deliberately the least "
            "privileged role."
        ),
        capabilities=frozenset({Capability.KNOWLEDGE_SEARCH}),
    ),
)

_BY_ROLE: Final[dict[AgentRole, AgentSpec]] = {spec.role: spec for spec in AGENT_ROSTER}


def capabilities_for(role: AgentRole) -> frozenset[Capability]:
    """Return the capabilities granted to ``role``.

    Raises:
        KeyError: if ``role`` is not in the roster, which means an agent was added without a
            declared capability set. Failing here is better than granting nothing silently —
            an agent with no capabilities would look like a permissions bug at runtime.
    """
    return _BY_ROLE[role].capabilities


def describe(role: AgentRole) -> str:
    """Return the human-facing description of ``role``."""
    return _BY_ROLE[role].description


def is_wired(role: AgentRole) -> bool:
    """Whether ``role`` is scheduled by the orchestrator today."""
    return _BY_ROLE[role].wired
