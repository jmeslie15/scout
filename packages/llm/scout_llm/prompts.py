"""Prompt versioning (spec section 39).

Prompts are versioned artefacts, not string literals buried in Python. A run records the
version *and* the digest of the exact text it used, so "why did the reviewer behave
differently on Tuesday?" has an answer that does not depend on the git log.

Layout (spec section 39)::

    prompts/<agent_role>/<version>.md      e.g. prompts/code_reviewer/v1.md

Rendering is **strict**, because silent substitution is exactly the framework magic Rule 13
forbids:

* a placeholder with no supplied value raises :class:`PromptRenderError`;
* a variable supplied that the template never mentions also raises — passing
  ``{"dif": ...}`` when the template says ``$diff`` is a typo, and silently ignoring it
  would send a prompt with an empty gap in it while looking like success;
* a malformed placeholder is rejected at **load** time, not at render time.

Placeholder syntax is :class:`string.Template` (``$name`` / ``${name}``, ``$$`` for a
literal dollar). It was chosen over ``str.format`` or ``{{ }}`` because prompt bodies
routinely contain JSON examples with braces, and a brace-based renderer forces every such
example to be escaped — the kind of papercut that ends with someone "fixing" the renderer
to be lenient.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from string import Template

from pydantic import BaseModel, ConfigDict, Field

from scout_domain.enums import AgentRole
from scout_llm.errors import PromptNotFoundError, PromptRenderError
from scout_llm.observability import get_logger

__all__ = ["PromptLibrary", "PromptTemplate"]

logger = get_logger(__name__)

#: A prompt version is a filename stem, and the accepted shapes are exactly the ones that
#: denote an ordered revision: ``v1``, ``1``, ``v1.2``, or an ISO date ``2024-06-01``.
#:
#: The pattern is deliberately narrow for two reasons. It stops ``../secrets`` from escaping
#: the prompts directory, and it keeps a stray ``README.md`` from being listed as a version
#: or — worse — becoming ``latest()``. A file that is not a version simply is not a prompt.
_VERSION_PATTERN = re.compile(r"^(?:v?\d+(?:\.\d+)*|\d{4}-\d{2}-\d{2})$")

#: Numeric version shapes, so ``v2`` sorts before ``v10`` and ``v1.2`` before ``v1.10``.
_NUMERIC_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)$")

_PROMPT_SUFFIX = ".md"


class PromptTemplate(BaseModel):
    """One versioned prompt body plus its identity.

    ``sha256`` is the digest of the **body as written on disk** (unrendered): it identifies
    the artefact. The digest of the *rendered* text is recorded separately on
    ``ModelRequestRecord.prompt_digest``, because that is what the model actually saw.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: AgentRole
    version: str
    body: str
    sha256: str = Field(min_length=1, max_length=128)
    placeholders: frozenset[str] = Field(default_factory=frozenset)

    def render(self, **variables: str) -> str:
        """Render the body, raising :class:`PromptRenderError` on any mismatch.

        Raises:
            PromptRenderError: a placeholder has no value, a supplied variable is unused,
                or a supplied value is not a string.
        """
        supplied = set(variables)
        missing = sorted(self.placeholders - supplied)
        unused = sorted(supplied - self.placeholders)
        if missing or unused:
            problems: list[str] = []
            if missing:
                problems.append(f"missing values for {missing}")
            if unused:
                problems.append(f"unused variables {unused}")
            raise PromptRenderError(
                f"cannot render prompt {self.role.value}/{self.version}: " + "; ".join(problems),
                role=self.role.value,
                version=self.version,
                missing=missing,
                unused=unused,
            )

        for name, value in variables.items():
            if not isinstance(value, str):
                raise PromptRenderError(
                    f"prompt variable {name!r} for {self.role.value}/{self.version} "
                    f"must be a string, got {type(value).__name__}",
                    role=self.role.value,
                    version=self.version,
                    variable=name,
                )

        return Template(self.body).substitute(variables)


class PromptLibrary:
    """Reads prompt templates from a ``prompts/<role>/`` tree.

    The root is injected rather than discovered so that tests can point at ``tmp_path`` and
    so that a missing tree is a clear runtime error instead of an import-time surprise.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The prompts directory this library reads from."""
        return self._root

    def path_for(self, role: AgentRole, version: str) -> Path:
        """Return the expected path for ``role``/``version`` without touching the disk."""
        return self._root / role.value / f"{version}{_PROMPT_SUFFIX}"

    def roles(self) -> list[AgentRole]:
        """Return the roles that have a prompt directory, in enum order."""
        if not self._root.is_dir():
            return []
        present = {entry.name for entry in self._root.iterdir() if entry.is_dir()}
        return [role for role in AgentRole if role.value in present]

    def versions(self, role: AgentRole) -> list[str]:
        """Return the available versions for ``role``, oldest first."""
        directory = self._root / role.value
        if not directory.is_dir():
            return []
        stems = [
            entry.name[: -len(_PROMPT_SUFFIX)]
            for entry in directory.iterdir()
            if entry.is_file() and entry.name.endswith(_PROMPT_SUFFIX)
        ]
        return sorted((stem for stem in stems if _VERSION_PATTERN.match(stem)), key=_version_key)

    def load(self, role: AgentRole, version: str) -> PromptTemplate:
        """Load ``prompts/<role>/<version>.md``.

        Raises:
            PromptNotFoundError: the version is missing, malformed, or the prompts tree
                does not exist. The message lists the versions that *do* exist.
        """
        if not _VERSION_PATTERN.match(version):
            raise self._not_found(role, version, reason="not a valid prompt version name")

        path = self.path_for(role, version)
        if not path.is_file():
            reason = (
                f"prompts directory {self._root} does not exist"
                if not self._root.is_dir()
                else f"no prompt file at {path}"
            )
            raise self._not_found(role, version, reason=reason)

        body = path.read_text(encoding="utf-8")
        placeholders = _validate_placeholders(body, role=role, version=version)
        return PromptTemplate(
            role=role,
            version=version,
            body=body,
            sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            placeholders=placeholders,
        )

    def latest(self, role: AgentRole) -> PromptTemplate:
        """Load the highest version available for ``role``.

        Raises:
            PromptNotFoundError: no versions exist for the role.
        """
        available = self.versions(role)
        if not available:
            raise self._not_found(role, "<latest>", reason="no prompt versions exist")
        return self.load(role, available[-1])

    def _not_found(self, role: AgentRole, version: str, *, reason: str) -> PromptNotFoundError:
        available = self.versions(role)
        listing = ", ".join(available) if available else "(none)"
        return PromptNotFoundError(
            f"prompt {role.value}/{version} is unavailable: {reason}. "
            f"Available versions for {role.value}: {listing}",
            role=role.value,
            version=version,
            available_versions=available,
            prompts_root=str(self._root),
        )


def _version_key(version: str) -> tuple[int, tuple[int, ...], str]:
    """Sort key giving natural version order rather than lexicographic order.

    ``v2`` must come before ``v10``, and ``v1.2`` before ``v1.10``; plain string sorting gets
    both wrong. Non-numeric versions (ISO dates, custom labels) sort after numeric ones, in
    lexicographic order — which for an ISO date is also chronological.
    """
    numeric = _NUMERIC_VERSION.match(version)
    if numeric:
        return (0, tuple(int(part) for part in numeric.group(1).split(".")), version)
    return (1, (0,), version)


def _validate_placeholders(
    body: str,
    *,
    role: AgentRole,
    version: str,
) -> frozenset[str]:
    """Return the placeholder names in ``body``, rejecting malformed ``$`` usage.

    Failing at load time means a broken template is caught when it is registered, not
    halfway through a verification run.
    """
    template = Template(body)
    pattern = Template.pattern
    if pattern is not None and not template.is_valid():
        offenders = [
            body[match.start() : match.start() + 16]
            for match in pattern.finditer(body)
            if match.group("invalid") is not None
        ]
        raise PromptRenderError(
            f"prompt template {role.value}/{version} contains malformed placeholders: "
            f"{offenders}. Use $name or ${{name}}, and $$ for a literal dollar sign.",
            role=role.value,
            version=version,
            offenders=offenders,
        )

    return frozenset(template.get_identifiers())
