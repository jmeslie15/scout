"""Contract between a prompt template and the call site that renders it.

This file exists because of a real bug.

Scout's prompt renderer is deliberately strict: it raises if a template references a variable
the caller did not supply **and** if the caller supplies a variable the template does not
reference. That second rule is the valuable one — a silent formatter would drop an unused value
and the first anyone would notice is a prompt that quietly stopped mentioning the diff.

The cost of that strictness is a contract: a template and its call site must agree on the exact
variable set. When they disagree the failure mode is *not* obviously a bug. In the incident that
prompted this file, `prompts/repository_analyzer/v1.md` declared no variables while
`llm_analysis._prompt_variables()` supplied thirteen; the renderer raised, the stage caught it,
and repository analysis **silently fell back to a deterministic narrative**. The platform kept
working, the job reached a report, and the only trace was one `info` log line. A change that
degrades output without failing is exactly the class of defect a contract test is for.

So: the variable set of every wired role is asserted against the call site itself, not against a
copy of the names in this file. Adding a variable to the call site without adding it to the
template fails here, and so does the reverse.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from scout_domain.enums import AgentRole
from scout_domain.models.repository import (
    ChangedFile,
    ChangeSet,
    ChangeType,
    DetectedService,
    RepositoryRef,
    RepositorySummary,
    TechnologyStack,
)
from scout_llm.prompts import PromptLibrary

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_ROOT = REPO_ROOT / "prompts"
PROMPT_VERSION = "v1"

#: Roles that currently have a call site in the orchestrator, mapped to the attribute path the
#: call site reads its variables from. When a new agent is wired, add it here — the test below
#: asserts the set is not stale in either direction.
WIRED_ROLES: dict[AgentRole, str] = {
    AgentRole.REPOSITORY_ANALYZER: "scout_orchestrator.llm_analysis:_prompt_variables",
}


def _by_role_value(role: AgentRole) -> str:
    """Sort key with an explicit signature.

    A bare ``lambda`` inside ``pytest.mark.parametrize`` is inferred as taking ``object``, which
    then makes ``role.value`` an attribute error under mypy strict.
    """
    return role.value


#: Every role, materialised so the two derived sets below are plain set operations with
#: unambiguous types. A generator inside ``frozenset(...)`` loses the element type here under
#: mypy strict, which then makes ``sorted(..., key=...)`` fail with "expected Callable[[object]]".
_ALL_ROLES: Final[frozenset[AgentRole]] = frozenset(AgentRole)

#: Roles with a template but no call site. Their templates must declare no placeholders.
UNWIRED_ROLES: Final[frozenset[AgentRole]] = _ALL_ROLES - frozenset(WIRED_ROLES)

#: Sorted once for stable parametrisation ids.
UNWIRED_ROLES_SORTED: Final[tuple[AgentRole, ...]] = tuple(
    sorted(UNWIRED_ROLES, key=_by_role_value)
)


@pytest.fixture(scope="module")
def library() -> PromptLibrary:
    return PromptLibrary(PROMPTS_ROOT)


def _sample_summary() -> RepositorySummary:
    """A fully populated summary, so every variable the call site builds is non-empty."""
    return RepositorySummary(
        repository=RepositoryRef(full_name="acme/widgets"),
        commit_sha="a" * 40,
        stack=TechnologyStack(
            languages=["python"],
            frameworks=["fastapi"],
            test_frameworks=["pytest"],
            package_managers=["uv"],
        ),
        services=[DetectedService(name="widget", path=".", kind="library")],
        file_count=12,
        entry_points=["main.py"],
        api_surfaces=["/widgets"],
        notes=["inventory was bounded at 1000 files"],
    )


def _sample_change_set() -> ChangeSet:
    return ChangeSet(
        base_sha="a" * 40,
        head_sha="b" * 40,
        files=[
            ChangedFile(path="widget.py", change_type=ChangeType.MODIFIED, additions=3, deletions=1)
        ],
    )


def _call_site_variables(attribute_path: str) -> frozenset[str]:
    """Resolve ``module:function`` and return the keys it supplies."""
    module_name, _, function_name = attribute_path.partition(":")
    module = __import__(module_name, fromlist=[function_name])
    function = getattr(module, function_name)
    supplied = function(summary=_sample_summary(), change_set=_sample_change_set())
    return frozenset(supplied)


class TestEveryRoleHasAPrompt:
    @pytest.mark.parametrize("role", list(AgentRole))
    def test_role_has_at_least_one_version(self, library: PromptLibrary, role: AgentRole) -> None:
        """A role with no template cannot be scheduled, so the gap should be visible."""
        versions = library.versions(role)
        assert versions, f"no prompt versions exist for {role.value} under prompts/{role.value}/"

    @pytest.mark.parametrize("role", list(AgentRole))
    def test_v1_is_loadable_and_addressable(self, library: PromptLibrary, role: AgentRole) -> None:
        template = library.load(role, PROMPT_VERSION)
        assert template.role is role
        assert template.version == PROMPT_VERSION
        assert template.body.strip(), f"{role.value} template is empty"
        assert len(template.sha256) == 64, "prompt identity must be a full sha256"

    @pytest.mark.parametrize("role", list(AgentRole))
    def test_placeholders_are_valid_identifiers(
        self, library: PromptLibrary, role: AgentRole
    ) -> None:
        """A placeholder must be usable as a keyword argument, since that is how it arrives."""
        for name in library.load(role, PROMPT_VERSION).placeholders:
            assert name.isidentifier(), f"{role.value}: {name!r} is not a valid identifier"
            assert not name.startswith("_"), f"{role.value}: {name!r} looks private"


class TestWiredRolesMatchTheirCallSite:
    """The assertion that would have caught the bug this file was written for."""

    @pytest.mark.parametrize(("role", "attribute_path"), sorted(WIRED_ROLES.items()))
    def test_declared_placeholders_equal_supplied_variables(
        self, library: PromptLibrary, role: AgentRole, attribute_path: str
    ) -> None:
        template = library.load(role, PROMPT_VERSION)
        supplied = _call_site_variables(attribute_path)

        missing_from_template = sorted(supplied - template.placeholders)
        missing_from_call_site = sorted(template.placeholders - supplied)

        # Split rather than combined so the failure message names which direction broke.
        assert not missing_from_template, (
            f"{role.value}: {attribute_path} supplies variables the template does not use: "
            f"{missing_from_template}. The strict renderer raises on unused variables, and the "
            "calling stage degrades to a fallback instead of failing loudly."
        )
        assert not missing_from_call_site, (
            f"{role.value}: the template uses variables the call site does not supply: "
            f"{missing_from_call_site}. The strict renderer raises on undefined variables."
        )

    @pytest.mark.parametrize(("role", "attribute_path"), sorted(WIRED_ROLES.items()))
    def test_template_renders_with_exactly_the_call_site_variables(
        self, library: PromptLibrary, role: AgentRole, attribute_path: str
    ) -> None:
        """Prove the happy path end to end, not just that the sets are equal."""
        template = library.load(role, PROMPT_VERSION)
        module_name, _, function_name = attribute_path.partition(":")
        module = __import__(module_name, fromlist=[function_name])
        supplied = getattr(module, function_name)(
            summary=_sample_summary(), change_set=_sample_change_set()
        )

        rendered = template.render(**supplied)

        assert rendered.strip()
        assert "$" not in rendered, "a placeholder survived rendering"
        # Every supplied value should actually appear, so a variable cannot be declared and
        # then quietly ignored inside the body.
        for name, value in supplied.items():
            if value:
                assert value in rendered, f"{role.value}: supplied {name!r} but it is unused"


class TestUnwiredRolesDeclareNoVariables:
    @pytest.mark.parametrize("role", UNWIRED_ROLES_SORTED)
    def test_template_declares_no_placeholders(
        self, library: PromptLibrary, role: AgentRole
    ) -> None:
        """Nothing supplies variables to these roles yet, so declaring one would be broken.

        When a stage is wired for one of these roles, move it into ``WIRED_ROLES`` above; this
        test then fails until that happens, which is the intended prompt to update the contract.
        """
        template = library.load(role, PROMPT_VERSION)
        assert template.placeholders == frozenset(), (
            f"{role.value} declares {sorted(template.placeholders)} but no call site supplies "
            "them yet. Either wire the call site and add the role to WIRED_ROLES, or remove the "
            "placeholders."
        )

    def test_every_role_is_classified(self) -> None:
        """No role may be silently omitted from both sets."""
        assert WIRED_ROLES.keys() | UNWIRED_ROLES == set(AgentRole)
        assert not (WIRED_ROLES.keys() & UNWIRED_ROLES)


class TestPromptHygiene:
    @pytest.mark.parametrize("role", list(AgentRole))
    def test_template_tells_the_agent_that_repository_content_is_untrusted(
        self, library: PromptLibrary, role: AgentRole
    ) -> None:
        """Every template must state the trust boundary, because prompt injection is real.

        This is a documentation assertion, not a security control — the control is that
        repository text arrives as a variable rather than as instruction text. But a template
        that forgets to say so invites the model to treat a code comment as an order.
        """
        body = library.load(role, PROMPT_VERSION).body.lower()
        assert "untrusted" in body, f"{role.value} template does not state the trust boundary"
        assert any(
            phrase in body
            for phrase in ("not instruction", "ignore it", "directing your behaviour")
        ), f"{role.value} template does not tell the agent what to do with injected instructions"

    @pytest.mark.parametrize("role", list(AgentRole))
    def test_template_states_the_evidence_requirement(
        self, library: PromptLibrary, role: AgentRole
    ) -> None:
        """Spec sections 32 and 34: no free-form claims, every claim cites evidence.

        The Pydantic models enforce this. The prompt restates it so the model does not have to
        learn the contract by failing validation.
        """
        body = library.load(role, PROMPT_VERSION).body.lower()
        assert "evidence" in body or "structured" in body, (
            f"{role.value} template does not mention evidence or structured output"
        )

    def test_library_rejects_a_missing_version_with_an_actionable_error(
        self, library: PromptLibrary
    ) -> None:
        from scout_llm.errors import PromptNotFoundError

        with pytest.raises(PromptNotFoundError):
            library.load(AgentRole.CODE_REVIEWER, "v999")
