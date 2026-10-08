"""Reasoning agents for Scout.

This package is the designated home for the agent layer described in spec sections 31, 32, 40,
and 54: each agent receives an explicit :class:`~scout_domain.models.agent.AgentContext` and
returns an explicit :class:`~scout_domain.models.agent.AgentResult`. Both are validated Pydantic
models, so a malformed model response fails a contract test rather than silently degrading a
report, and free-form text is never an acceptable agent interface.

What is here today
------------------
:mod:`scout_agents.roster` — the canonical roster: which roles exist, what each is for, and the
capabilities each is granted. The dashboard and the capability check both read it, so least
privilege is declared in exactly one place.

Known deviation, recorded deliberately
--------------------------------------
The orchestrator currently calls the LLM gateway **directly** for repository analysis
(``scout_orchestrator.llm_analysis``) rather than constructing an ``AgentContext`` and routing
through an ``AgentRunner`` here. That is a real architectural gap, not an oversight: the
first vertical slice (spec section 83) needs no reasoning agents, so the agent layer was not
built ahead of the milestone that requires it.

It must be closed in Milestone 5, when the code-review agent is introduced. The shape of the
fix is:

1. An ``AgentRunner`` here that takes an ``AgentContext``, asserts the role's capabilities via
   :func:`scout_agents.roster.capabilities_for`, renders the versioned prompt, calls the
   gateway, validates the response into an ``AgentResult``, and persists an ``AgentRun``
   recording the prompt version and model.
2. ``AgentContext.capabilities`` populated from the roster, with the runner calling
   ``AgentContext.requires()`` before any capability-scoped action.
3. ``llm_analysis`` reduced to building the context and delegating.

The contract tests that would catch a regression are the ones asserting every agent returns an
``AgentResult`` and that a role without a capability cannot perform the corresponding action.
"""

from __future__ import annotations

from scout_agents.roster import (
    AGENT_ROSTER,
    AgentSpec,
    capabilities_for,
    describe,
    is_wired,
)

__version__ = "0.1.0"

__all__ = [
    "AGENT_ROSTER",
    "AgentSpec",
    "__version__",
    "capabilities_for",
    "describe",
    "is_wired",
]
