"""Typed views over GitHub's REST payloads.

Why these exist: the alternative is handing raw ``dict[str, Any]`` around the platform,
which defeats type checking and turns a renamed GitHub field into a runtime ``KeyError``
far from the request that produced it. Each model declares exactly the fields Scout
consumes, with aliases matching GitHub's wire format.

Two deliberate properties:

* ``extra="ignore"`` — GitHub adds fields to its payloads regularly (for example
  ``auto_merge``, ``active_lock_reason``, ``rebaseable``). Forbidding extras would make
  Scout break on a payload change that does not affect Scout. This is the one place
  where the domain's ``extra="forbid"`` posture is intentionally not mirrored, because
  these models describe an external contract Scout does not control.
* Only fields Scout actually reads are declared, with defaults, so a partial payload
  (a field GitHub omits on older API versions or on Enterprise Server) parses instead of
  crashing. Callers that need a field to be *present* validate it explicitly; see
  :func:`scout_github.webhooks.parse_pull_request_event`.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from scout_github.errors import GitHubError

__all__ = [
    "GitHubBranchRef",
    "GitHubCheckOutput",
    "GitHubCheckRun",
    "GitHubCommit",
    "GitHubCompare",
    "GitHubFile",
    "GitHubFileContents",
    "GitHubGitActor",
    "GitHubGitCommit",
    "GitHubIssue",
    "GitHubIssueComment",
    "GitHubModel",
    "GitHubPullRequest",
    "GitHubRepository",
    "GitHubUser",
]


class GitHubModel(BaseModel):
    """Base for inbound GitHub API payloads.

    ``extra="ignore"`` keeps Scout working when GitHub adds fields; ``populate_by_name``
    lets callers construct models with the Python field names used in code and tests.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    def evolve(self, **changes: Any) -> Self:
        """Return a validated copy with ``changes`` applied.

        Mirrors :meth:`scout_domain.models.base.ScoutModel.evolve` so code that handles
        both domain objects and GitHub payloads does not need two idioms.
        """
        return type(self).model_validate({**self.model_dump(), **changes})


class GitHubUser(GitHubModel):
    """A GitHub account, reduced to what Scout needs to attribute a change."""

    login: str = ""
    id: int | None = None
    type: str | None = None
    html_url: str | None = None


class GitHubRepository(GitHubModel):
    """A repository as returned by ``GET /repos/{owner}/{repo}``."""

    id: int | None = None
    full_name: str
    name: str = ""
    owner: GitHubUser | None = None
    default_branch: str = "main"
    private: bool = False
    fork: bool = False
    archived: bool = False
    html_url: str | None = None


class GitHubBranchRef(GitHubModel):
    """One side of a pull request (the ``head`` or ``base`` object)."""

    ref: str = ""
    sha: str = ""
    label: str | None = None
    user: GitHubUser | None = None
    repo: GitHubRepository | None = None


class GitHubPullRequest(GitHubModel):
    """A pull request as returned by the pulls endpoints."""

    number: int
    title: str = ""
    #: GitHub sends ``null`` for an empty description; ``None`` is preserved rather than
    #: coerced to ``""`` so callers can tell "not provided" from "provided empty".
    body: str | None = None
    state: str = "open"
    #: Absent on some payloads (and always absent on events that predate drafts), hence
    #: the default. Read it as ``pr.draft is True``, never as truthiness.
    draft: bool = False
    merged: bool = False
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None
    html_url: str | None = None
    user: GitHubUser | None = None
    base: GitHubBranchRef = Field(default_factory=GitHubBranchRef)
    head: GitHubBranchRef = Field(default_factory=GitHubBranchRef)
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    commits: int = 0
    author_association: str | None = None


class GitHubFile(GitHubModel):
    """One entry of a PR-files or compare response.

    ``patch`` is absent for binary files, for rename/copy entries with no textual
    change, and for diffs GitHub considers too large. See
    :func:`scout_github.diff.is_binary_file` for how Scout interprets that.
    """

    filename: str
    status: str
    previous_filename: str | None = None
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    patch: str | None = None
    sha: str | None = None
    blob_url: str | None = None
    raw_url: str | None = None
    contents_url: str | None = None


class GitHubGitActor(GitHubModel):
    """The ``author``/``committer`` block of a git commit object."""

    name: str | None = None
    email: str | None = None
    date: datetime | None = None


class GitHubGitCommit(GitHubModel):
    """The nested ``commit`` object: the git-level commit, not the API wrapper."""

    message: str = ""
    author: GitHubGitActor | None = None
    committer: GitHubGitActor | None = None


class GitHubCommitRef(GitHubModel):
    """A parent-commit pointer."""

    sha: str
    html_url: str | None = None


class GitHubCommit(GitHubModel):
    """A commit as returned by ``GET /repos/{owner}/{repo}/commits/{ref}``."""

    sha: str
    commit: GitHubGitCommit = Field(default_factory=GitHubGitCommit)
    html_url: str | None = None
    author: GitHubUser | None = None
    parents: list[GitHubCommitRef] = Field(default_factory=list)

    @property
    def message(self) -> str:
        """The commit message, flattened off the nested git object."""
        return self.commit.message


class GitHubCompare(GitHubModel):
    """The result of ``GET /repos/{owner}/{repo}/compare/{base}...{head}``."""

    status: str = "unknown"
    ahead_by: int = 0
    behind_by: int = 0
    total_commits: int = 0
    html_url: str | None = None
    commits: list[GitHubCommit] = Field(default_factory=list)
    files: list[GitHubFile] = Field(default_factory=list)
    base_commit: GitHubCommit | None = None
    merge_base_commit: GitHubCommit | None = None


class GitHubCheckOutput(GitHubModel):
    """The ``output`` block of a check run: what the PR page actually renders."""

    title: str | None = None
    summary: str | None = None
    text: str | None = None


class GitHubCheckRun(GitHubModel):
    """A check run as returned by the check-runs endpoints."""

    id: int
    name: str = ""
    head_sha: str = ""
    status: str = "queued"
    conclusion: str | None = None
    details_url: str | None = None
    external_id: str | None = None
    html_url: str | None = None
    output: GitHubCheckOutput | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class GitHubIssue(GitHubModel):
    """An issue as returned by ``GET /repos/{owner}/{repo}/issues/{number}``.

    A pull request is also an issue in GitHub's model, so ``pull_request`` is set when
    this number is really a PR; Scout uses that to avoid treating a PR as a linked
    requirement issue.
    """

    number: int
    title: str = ""
    body: str | None = None
    state: str = "open"
    html_url: str | None = None
    user: GitHubUser | None = None
    pull_request: dict[str, Any] | None = None
    labels: list[dict[str, Any]] = Field(default_factory=list)


class GitHubIssueComment(GitHubModel):
    """A comment as returned by the issue-comments endpoints."""

    id: int
    body: str | None = None
    html_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    user: GitHubUser | None = None


class GitHubFileContents(GitHubModel):
    """The contents API response for a single file.

    GitHub returns ``content`` base64-encoded *with embedded newlines* and does not
    promise padding is meaningful, so decoding must tolerate both. ``decoded_bytes``
    does the tolerant decode in one place rather than at every call site.
    """

    type: str = "file"
    encoding: str = "base64"
    size: int = 0
    name: str = ""
    path: str = ""
    sha: str = ""
    #: Base64 text as GitHub sent it (newlines included).
    content: str = ""
    url: str | None = None
    html_url: str | None = None
    download_url: str | None = None
    target: str | None = None

    def decoded_bytes(self) -> bytes:
        """Decode ``content`` to bytes.

        Raises:
            GitHubError: When the payload is not decodable base64. This surfaces as a
                Scout error rather than a bare ``binascii.Error`` so callers can
                classify and report it like any other GitHub problem.
        """
        try:
            return base64.b64decode(self.content)
        except (binascii.Error, ValueError) as exc:
            raise GitHubError(
                "GitHub returned file content that is not valid base64",
                path=self.path,
            ) from exc

    def decoded_text(self, encoding: str = "utf-8") -> str:
        """Decode ``content`` to text.

        Args:
            encoding: Text encoding; UTF-8 unless a caller knows better from the file.

        Raises:
            GitHubError: When the content is not decodable base64 or not decodable text
                in ``encoding`` (a binary file fetched by mistake).
        """
        raw = self.decoded_bytes()
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise GitHubError(
                f"file content at {self.path!r} is not valid {encoding} text; "
                "it is probably binary",
                path=self.path,
                encoding=encoding,
            ) from exc
