"""Repository, technology-stack, and change-analysis models (spec sections 13-16)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from scout_domain.models.base import ScoutModel, utcnow

__all__ = [
    "ChangeGraph",
    "ChangeSet",
    "ChangeType",
    "ChangedFile",
    "DetectedService",
    "RepositoryRef",
    "RepositorySummary",
    "TechnologyStack",
]


class ChangeType(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"
    TYPE_CHANGED = "type_changed"
    UNMERGED = "unmerged"


class RepositoryRef(ScoutModel):
    """A GitHub repository identity.

    ``installation_id`` is *never* placed in agent context: it is a credential handle
    for the control plane only (spec sections 54 and 55).
    """

    #: ``owner/name``, the canonical human-readable identity.
    full_name: str = Field(min_length=3, max_length=200, pattern=r"^[^/\s]+/[^/\s]+$")
    #: Stable numeric GitHub repository id. Survives renames, unlike ``full_name``.
    github_id: int | None = Field(default=None, ge=1)
    default_branch: str = Field(default="main", max_length=255)
    #: True for repositories Scout is permitted to clone and analyse.
    is_active: bool = True

    @property
    def owner(self) -> str:
        return self.full_name.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.full_name.split("/", 1)[1]


class TechnologyStack(ScoutModel):
    """What Scout detected in a repository (spec section 15)."""

    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    test_frameworks: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)
    build_tools: list[str] = Field(default_factory=list)
    ci_systems: list[str] = Field(default_factory=list)
    containerized: bool = False
    has_infrastructure_as_code: bool = False


class DetectedService(ScoutModel):
    """An application, service, or deployable unit inside the repository."""

    name: str = Field(min_length=1, max_length=200)
    path: str = Field(min_length=1, max_length=1024)
    kind: str = Field(default="unknown", max_length=100)
    languages: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    #: True when Scout identified a test suite belonging to this service.
    has_tests: bool = False


class RepositorySummary(ScoutModel):
    """Machine-readable understanding of the repository (spec section 15).

    This is the object handed to agents as ``AgentContext.repository_summary``. It is
    deliberately bounded: agents never receive the repository itself, only this summary
    plus selectively retrieved files (spec section 17).
    """

    repository: RepositoryRef
    commit_sha: str = Field(min_length=7, max_length=64)
    stack: TechnologyStack = Field(default_factory=TechnologyStack)
    services: list[DetectedService] = Field(default_factory=list)

    #: Coarse counts, useful for bounding context and for diagrams.
    file_count: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)

    entry_points: list[str] = Field(default_factory=list)
    api_surfaces: list[str] = Field(default_factory=list)
    auth_mechanisms: list[str] = Field(default_factory=list)
    authorization_mechanisms: list[str] = Field(default_factory=list)
    external_dependencies: list[str] = Field(default_factory=list)
    documentation_paths: list[str] = Field(default_factory=list)
    infrastructure_paths: list[str] = Field(default_factory=list)
    test_paths: list[str] = Field(default_factory=list)
    migration_paths: list[str] = Field(default_factory=list)

    #: Free-form notes produced by the analyzer agent, kept short. Anything long
    #: belongs in an artifact so the context object stays within a token budget.
    notes: list[str] = Field(default_factory=list)
    analyzed_at: datetime = Field(default_factory=utcnow)


class ChangedFile(ScoutModel):
    """One entry of the git diff for a pull request (spec section 16)."""

    path: str = Field(min_length=1, max_length=1024)
    change_type: ChangeType
    previous_path: str | None = Field(default=None, max_length=1024)

    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    #: Artifact holding this file's patch. Patches are never inlined into the DB.
    patch_artifact_id: str | None = None

    #: True when the file is binary; such files are excluded from AI review.
    is_binary: bool = False

    @property
    def churn(self) -> int:
        return self.additions + self.deletions


class ChangeSet(ScoutModel):
    """Everything that changed between base and head (spec section 16).

    The commit SHAs are pinned here. Spec section 13 is emphatic: never analyse an
    unpinned moving branch. A verification report is only meaningful if it names the
    exact commit it verified.
    """

    base_sha: str = Field(min_length=7, max_length=64)
    head_sha: str = Field(min_length=7, max_length=64)
    files: list[ChangedFile] = Field(default_factory=list)
    #: Artifact holding the full unified diff.
    diff_artifact_id: str | None = None
    #: Artifact holding the list of changed paths only, for cheap re-reads.
    changed_paths_artifact_id: str | None = None
    computed_at: datetime = Field(default_factory=utcnow)

    @property
    def total_additions(self) -> int:
        return sum(file.additions for file in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(file.deletions for file in self.files)

    @property
    def changed_paths(self) -> list[str]:
        return [file.path for file in self.files]

    def contains(self, path_prefix: str) -> bool:
        """Return ``True`` if any changed path starts with ``path_prefix``."""
        return any(file.path.startswith(path_prefix) for file in self.files)


class ChangeGraphNode(ScoutModel):
    """A node in the change graph, e.g. an endpoint, service, or schema."""

    id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=300)
    kind: str = Field(min_length=1, max_length=100)
    path: str | None = Field(default=None, max_length=1024)


class ChangeGraphEdge(ScoutModel):
    """A directed relationship between two change-graph nodes."""

    source_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=200)
    #: e.g. ``calls``, ``reads_table``, ``renders``.
    relation: str = Field(min_length=1, max_length=100)


class ChangeGraph(ScoutModel):
    """How the change propagates through the system (spec section 16).

    Example from the spec::

        PR -> API endpoint -> service -> database query
    """

    nodes: list[ChangeGraphNode] = Field(default_factory=list)
    edges: list[ChangeGraphEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_edges_reference_nodes(self) -> ChangeGraph:
        known = {node.id for node in self.nodes}
        dangling = [
            edge
            for edge in self.edges
            if edge.source_id not in known or edge.target_id not in known
        ]
        if dangling:
            rendered = ", ".join(f"{e.source_id}->{e.target_id}" for e in dangling[:5])
            raise ValueError(f"change graph edges reference unknown nodes: {rendered}")
        return self

    def neighbours(self, node_id: str) -> list[str]:
        """Return the ids directly reachable from ``node_id``."""
        return [edge.target_id for edge in self.edges if edge.source_id == node_id]
