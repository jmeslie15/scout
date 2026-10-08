"""The GitHub client interface and its two implementations (Rule 2, spec sections 9, 10, 51).

Every GitHub interaction in Scout goes through the :class:`GitHubClient` protocol:

* :class:`HttpGitHubClient` is production. It speaks the REST API with an installation
  token, maps HTTP failures onto typed Scout errors, and **never retries** — backoff and
  retry limits belong to the orchestrator (spec section 51), so a retry hidden inside an
  HTTP client would be invisible to the job's audit trail.
* :class:`FakeGitHubClient` is in-memory and selected when ``SCOUT_GITHUB_APP_ID`` is
  blank, which is the shipped default in ``.env.example``. It records every check run and
  comment it is asked to create, so the whole platform is exercisable end-to-end offline
  and tests can assert on what Scout would have told a human.

Because the protocol is ``runtime_checkable``, the API and orchestrator can hold either
implementation without importing httpx, and configuration (not code) decides which one a
deployment gets.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from scout_github._http import (
    API_VERSION,
    DEFAULT_API_BASE_URL,
    DEFAULT_USER_AGENT,
    MAX_PAGES,
    PER_PAGE,
    build_request_headers,
    next_page_url,
    raise_for_github_status,
)
from scout_github._logging import get_logger
from scout_github.checks import (
    CheckRunConclusion,
    CheckRunStatus,
    build_check_run_payload,
    resolve_check_state,
)
from scout_github.errors import GitHubError, GitHubNotFoundError, GitHubTransientError
from scout_github.models import (
    GitHubBranchRef,
    GitHubCheckOutput,
    GitHubCheckRun,
    GitHubCommit,
    GitHubCompare,
    GitHubFile,
    GitHubFileContents,
    GitHubGitActor,
    GitHubGitCommit,
    GitHubIssue,
    GitHubIssueComment,
    GitHubPullRequest,
    GitHubRepository,
    GitHubUser,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for typing only
    from scout_github.auth import InstallationTokenProvider

__all__ = [
    "DEFAULT_FAKE_BASE_SHA",
    "DEFAULT_FAKE_HEAD_SHA",
    "FakeGitHubClient",
    "GitHubClient",
    "GitHubCommentRecord",
    "HttpGitHubClient",
]

logger = get_logger(__name__)

#: Deterministic placeholder SHAs for fake data. 40 hex characters, matching git's
#: SHA-1 object format, so anything that validates SHA shape keeps working.
DEFAULT_FAKE_BASE_SHA = "a" * 40
DEFAULT_FAKE_HEAD_SHA = "b" * 40

#: ``owner/name`` splits into exactly two segments.
_REPO_FULL_NAME_SEGMENTS = 2


@runtime_checkable
class GitHubClient(Protocol):
    """The explicit GitHub interface (Rule 2).

    Ten methods, all ``async``, all returning typed payloads rather than raw JSON. The
    protocol is intentionally narrow: it exposes exactly what verification needs and
    nothing more. There is no merge, no push, and no branch write — ``Capability`` has
    no such grant, and widening this interface would be how one appears.
    """

    async def get_repository(self, full_name: str) -> GitHubRepository:
        """Return repository metadata for ``owner/name``."""
        ...

    async def get_pull_request(self, full_name: str, number: int) -> GitHubPullRequest:
        """Return the pull request, including its pinned head and base SHAs."""
        ...

    async def list_pull_request_files(self, full_name: str, number: int) -> list[GitHubFile]:
        """Return every changed file of the pull request, following pagination."""
        ...

    async def get_commit(self, full_name: str, sha: str) -> GitHubCommit:
        """Return one commit, including its message (requirement material)."""
        ...

    async def compare_commits(self, full_name: str, base: str, head: str) -> GitHubCompare:
        """Return the comparison between two commits or refs."""
        ...

    async def create_check_run(
        self,
        full_name: str,
        head_sha: str,
        name: str,
        status: CheckRunStatus | str = CheckRunStatus.COMPLETED,
        conclusion: CheckRunConclusion | str | None = None,
        title: str = "",
        summary: str = "",
        details_url: str | None = None,
        external_id: str | None = None,
    ) -> GitHubCheckRun:
        """Create the check run that carries Scout's conclusion onto the pull request."""
        ...

    async def update_check_run(
        self,
        full_name: str,
        check_run_id: int,
        *,
        status: CheckRunStatus | str | None = None,
        conclusion: CheckRunConclusion | str | None = None,
        title: str | None = None,
        summary: str | None = None,
        details_url: str | None = None,
        external_id: str | None = None,
    ) -> GitHubCheckRun:
        """Update a check run, e.g. move it from ``in_progress`` to ``completed``.

        Follows PATCH semantics: an argument left as ``None`` is not sent, so the field
        keeps its current value on GitHub. Supplying ``conclusion`` without ``status``
        completes the run, because GitHub only accepts a conclusion on a completed run.
        """
        ...

    async def create_pull_request_comment(
        self, full_name: str, number: int, body: str
    ) -> GitHubIssueComment:
        """Post a comment on the pull request. Never used for verdicts."""
        ...

    async def get_issue(self, full_name: str, number: int) -> GitHubIssue:
        """Return an issue, used to read linked requirements (spec section 24)."""
        ...

    async def get_file_contents(
        self, full_name: str, path: str, ref: str | None = None
    ) -> GitHubFileContents:
        """Return one file's contents at ``ref``."""
        ...


@dataclass(frozen=True, slots=True)
class _StaticTokenProvider:
    """Wraps a fixed token so tests can build a client without an authenticator."""

    token: str

    async def get_installation_token(self, installation_id: int) -> str:
        """Return the fixed token, ignoring the installation id by design."""
        del installation_id
        return self.token


class HttpGitHubClient:
    """httpx-backed GitHub REST client used in production.

    One instance serves one installation, because the installation id determines which
    token authorises the call. Token acquisition is delegated to an
    :class:`~scout_github.auth.InstallationTokenProvider`, whose cache means the token is
    normally retrieved without a network round trip.

    Error mapping is centralised in :mod:`scout_github._http`; read
    :func:`~scout_github._http.raise_for_github_status` for the status table. This class
    never retries and never sleeps (spec section 51).
    """

    def __init__(
        self,
        *,
        installation_id: int,
        tokens: InstallationTokenProvider | str,
        base_url: str = DEFAULT_API_BASE_URL,
        timeout_seconds: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        api_version: str = API_VERSION,
        user_agent: str = DEFAULT_USER_AGENT,
        max_pages: int = MAX_PAGES,
    ) -> None:
        """Configure a client for one GitHub App installation.

        Args:
            installation_id: Installation whose token authorises every call.
            tokens: Token provider, or a fixed token string (tests, and deployments that
                inject an already-minted token).
            base_url: API root; override for GitHub Enterprise Server
                (``https://ghe.example.com/api/v3``).
            timeout_seconds: Per-request timeout when this instance owns its client.
            http_client: Optional injected client, e.g. a shared connection pool. When
                supplied, the caller owns its lifetime.
            api_version: ``X-GitHub-Api-Version`` value.
            user_agent: ``User-Agent`` value.
            max_pages: Cap on pages followed for list endpoints.

        Raises:
            GitHubError: When ``installation_id`` is not positive.
        """
        self._installation_id = _require_positive(installation_id, field="installation_id")
        self._static_token = _StaticTokenProvider(token=tokens) if isinstance(tokens, str) else None
        self._token_provider = tokens if not isinstance(tokens, str) else None
        self._base_url = base_url.strip().rstrip("/") or DEFAULT_API_BASE_URL
        self._api_version = api_version
        self._user_agent = user_agent
        self._max_pages = max(1, max_pages)
        self._owns_http_client = http_client is None
        self._http = (
            http_client if http_client is not None else httpx.AsyncClient(timeout=timeout_seconds)
        )

    @property
    def base_url(self) -> str:
        """The configured API root."""
        return self._base_url

    async def get_repository(self, full_name: str) -> GitHubRepository:
        """Return repository metadata (``GET /repos/{owner}/{repo}``)."""
        response = await self._request("GET", f"/repos/{_repo_path(full_name)}")
        return _parse_model(GitHubRepository, response, resource=f"repository {full_name}")

    async def get_pull_request(self, full_name: str, number: int) -> GitHubPullRequest:
        """Return a pull request (``GET /repos/{owner}/{repo}/pulls/{number}``)."""
        pr_number = _require_positive(number, field="pull request number")
        response = await self._request(
            "GET",
            f"/repos/{_repo_path(full_name)}/pulls/{pr_number}",
        )
        return _parse_model(
            GitHubPullRequest,
            response,
            resource=f"pull request {full_name}#{number}",
        )

    async def list_pull_request_files(self, full_name: str, number: int) -> list[GitHubFile]:
        """Return every changed file, following ``Link: rel="next"`` pagination.

        Large pull requests are the normal case for the repositories Scout targets, and a
        truncated file list would understate the change set — which is exactly the
        understatement spec section 16 exists to prevent.
        """
        pr_number = _require_positive(number, field="pull request number")
        path = f"/repos/{_repo_path(full_name)}/pulls/{pr_number}/files"
        pages = await self._request_pages(path)
        files: list[GitHubFile] = []
        for page in pages:
            files.extend(
                _parse_records(
                    GitHubFile,
                    page,
                    resource=f"pull request {full_name}#{pr_number} files",
                )
            )
        return files

    async def get_commit(self, full_name: str, sha: str) -> GitHubCommit:
        """Return one commit (``GET /repos/{owner}/{repo}/commits/{sha}``)."""
        response = await self._request(
            "GET", f"/repos/{_repo_path(full_name)}/commits/{_require_ref(sha, field='commit sha')}"
        )
        return _parse_model(GitHubCommit, response, resource=f"commit {sha}")

    async def compare_commits(self, full_name: str, base: str, head: str) -> GitHubCompare:
        """Compare two commits (``GET /repos/{owner}/{repo}/compare/{base}...{head}``).

        ``base`` and ``head`` may be SHAs or refs. They are passed through with only
        surrounding whitespace removed: branch names legitimately contain ``/`` and
        percent-encoding a slash would change the ref GitHub compares.
        """
        base_ref = _require_ref(base, field="base ref")
        head_ref = _require_ref(head, field="head ref")
        response = await self._request(
            "GET",
            f"/repos/{_repo_path(full_name)}/compare/{base_ref}...{head_ref}",
        )
        return _parse_model(
            GitHubCompare,
            response,
            resource=f"compare {base_ref}...{head_ref}",
        )

    async def create_check_run(
        self,
        full_name: str,
        head_sha: str,
        name: str,
        status: CheckRunStatus | str = CheckRunStatus.COMPLETED,
        conclusion: CheckRunConclusion | str | None = None,
        title: str = "",
        summary: str = "",
        details_url: str | None = None,
        external_id: str | None = None,
    ) -> GitHubCheckRun:
        """Create a check run (``POST /repos/{owner}/{repo}/check-runs``).

        The request body is built by
        :func:`scout_github.checks.build_check_run_payload` so the same validation runs
        in production, in tests, and in the fake client.
        """
        body = build_check_run_payload(
            name=name,
            head_sha=head_sha,
            status=status,
            conclusion=conclusion,
            title=title,
            summary=summary,
            details_url=details_url,
            external_id=external_id,
        )
        response = await self._request(
            "POST",
            f"/repos/{_repo_path(full_name)}/check-runs",
            json_body=body,
        )
        return _parse_model(GitHubCheckRun, response, resource=f"check run {name!r}")

    async def update_check_run(
        self,
        full_name: str,
        check_run_id: int,
        *,
        status: CheckRunStatus | str | None = None,
        conclusion: CheckRunConclusion | str | None = None,
        title: str | None = None,
        summary: str | None = None,
        details_url: str | None = None,
        external_id: str | None = None,
    ) -> GitHubCheckRun:
        """Update a check run (``PATCH /repos/{owner}/{repo}/check-runs/{id}``).

        PATCH semantics, deliberately: an argument left as ``None`` is omitted from the
        request body, so completing a run does not erase the output recorded when it was
        created, and a summary-only update does not silently complete the run. Supplying
        ``conclusion`` without ``status`` implies completion, because GitHub rejects a
        conclusion on a run that is not completed.
        """
        run_id = _require_positive(check_run_id, field="check run id")
        body = _build_check_run_update(
            status=status,
            conclusion=conclusion,
            title=title,
            summary=summary,
            details_url=details_url,
            external_id=external_id,
        )
        response = await self._request(
            "PATCH",
            f"/repos/{_repo_path(full_name)}/check-runs/{run_id}",
            json_body=body,
        )
        return _parse_model(GitHubCheckRun, response, resource=f"check run {check_run_id}")

    async def create_pull_request_comment(
        self, full_name: str, number: int, body: str
    ) -> GitHubIssueComment:
        """Comment on a pull request (``POST /repos/{owner}/{repo}/issues/{n}/comments``).

        GitHub models PR comments as issue comments, so this is the issues endpoint. A
        comment is not a verdict: Scout's verdict is the check run, and comments are for
        questions that need a human (spec section 27).
        """
        pr_number = _require_positive(number, field="pull request number")
        response = await self._request(
            "POST",
            f"/repos/{_repo_path(full_name)}/issues/{pr_number}/comments",
            json_body={"body": body},
        )
        return _parse_model(
            GitHubIssueComment,
            response,
            resource=f"comment on {full_name}#{pr_number}",
        )

    async def get_issue(self, full_name: str, number: int) -> GitHubIssue:
        """Return an issue (``GET /repos/{owner}/{repo}/issues/{number}``)."""
        issue_number = _require_positive(number, field="issue number")
        response = await self._request(
            "GET", f"/repos/{_repo_path(full_name)}/issues/{issue_number}"
        )
        return _parse_model(GitHubIssue, response, resource=f"issue {full_name}#{number}")

    async def get_file_contents(
        self, full_name: str, path: str, ref: str | None = None
    ) -> GitHubFileContents:
        """Return one file's contents (``GET /repos/{owner}/{repo}/contents/{path}``).

        The contents endpoint answers with a JSON *array* for a directory. That is
        rejected explicitly, because silently parsing the first element would hand a
        caller a different file than the one it asked for.
        """
        cleaned_path = path.strip().lstrip("/")
        if not cleaned_path:
            raise GitHubError("file path must not be empty")
        params = {"ref": ref.strip()} if ref and ref.strip() else None
        response = await self._request(
            "GET",
            f"/repos/{_repo_path(full_name)}/contents/{quote(cleaned_path, safe='/')}",
            params=params,
        )
        payload = _parse_json(response, resource=f"file {path}")
        if isinstance(payload, list):
            raise GitHubError(
                f"{path!r} is a directory, not a file; the contents API returned a listing",
                path=path,
            )
        return _parse_model(GitHubFileContents, response, resource=f"file {path}")

    async def aclose(self) -> None:
        """Close the HTTP client if this instance owns it."""
        if self._owns_http_client:
            await self._http.aclose()

    async def __aenter__(self) -> HttpGitHubClient:
        """Support ``async with HttpGitHubClient(...) as client``."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close owned resources on context-manager exit."""
        await self.aclose()

    async def _installation_token(self) -> str:
        """Return the bearer token for the next request.

        SECURITY: the returned value is a credential. It is placed directly into the
        request headers and is never logged.
        """
        if self._static_token is not None:
            return self._static_token.token
        if self._token_provider is None:  # pragma: no cover - constructor guarantees one
            raise GitHubError("no GitHub token provider is configured")
        return await self._token_provider.get_installation_token(self._installation_id)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform one request against the configured API root, or raise a typed error."""
        return await self._request_url(
            method,
            f"{self._base_url}{path}",
            params=params,
            json_body=json_body,
        )

    async def _request_url(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform one request against an absolute URL, or raise a typed error.

        Transport failures become :class:`GitHubTransientError`; HTTP failures are mapped
        by :func:`~scout_github._http.raise_for_github_status`. Nothing is retried here.
        """
        token = await self._installation_token()
        headers = build_request_headers(
            token=token,
            api_version=self._api_version,
            user_agent=self._user_agent,
        )
        try:
            response = await self._http.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
        except httpx.TransportError as exc:
            raise GitHubTransientError(
                f"could not reach GitHub for {method} {url}",
                error=type(exc).__name__,
            ) from exc

        raise_for_github_status(response, resource=f"{method} {url}")
        return response

    async def _request_pages(self, path: str) -> list[Any]:
        """Fetch a list endpoint, following ``Link: rel="next"`` up to ``max_pages``.

        Query parameters are sent only on the first request: GitHub's ``next`` URL
        already carries them, and re-appending them would duplicate parameters.
        """
        payloads: list[Any] = []
        page_url: str | None = f"{self._base_url}{path}"
        first_request = True

        while page_url is not None:
            response = await self._request_url(
                "GET",
                page_url,
                params={"per_page": str(PER_PAGE)} if first_request else None,
            )
            first_request = False
            payloads.append(_parse_json(response, resource=path))
            page_url = next_page_url(response)

            if page_url is not None and len(payloads) >= self._max_pages:
                # Logged rather than raised: a truncated list is a defect in the report,
                # not a reason to fail a job that can still verify what it received. The
                # warning is only emitted when a further page really was offered.
                logger.warning(
                    "stopped following GitHub pagination at %s pages for %s",
                    self._max_pages,
                    path,
                )
                break

        return payloads


@dataclass(frozen=True, slots=True)
class GitHubCommentRecord:
    """A comment :class:`FakeGitHubClient` was asked to create.

    Why a separate ledger: GitHub's comment response does not echo the repository or pull
    request number, and those are exactly what a test asserts on. Keeping the extra
    context beside the response — rather than adding fake-only fields to
    :class:`GitHubIssueComment` — keeps the API model faithful to the real payload.
    """

    full_name: str
    number: int
    body: str
    comment: GitHubIssueComment

    @property
    def id(self) -> int:
        """The created comment's id, so the record can stand in for the comment itself."""
        return self.comment.id


class FakeGitHubClient:
    """In-memory :class:`GitHubClient` used when no GitHub App is configured.

    This is the default: ``.env.example`` ships a blank app id, which selects this
    implementation, so the entire platform — webhook to report to check run — is
    exercisable offline, in CI, and in a demo without a GitHub account (spec section 9).

    Contract, stated so tests can rely on it:

    * **Reads are strict.** Fetching something that was never registered raises
      :class:`GitHubNotFoundError`. A missing read is a test bug, and a fake that
      invents data would let that bug reach production.
    * **Writes are recorded, not required.** Check runs and comments are recorded even
      for a repository or pull request that was never registered, because a flow that
      reports back is worth testing on its own. Assert on :attr:`check_runs`,
      :attr:`check_run_updates`, and :attr:`comments`.
    """

    def __init__(self) -> None:
        """Create an empty fake with deterministic id sequences."""
        self._repositories: dict[str, GitHubRepository] = {}
        self._pull_requests: dict[tuple[str, int], GitHubPullRequest] = {}
        self._pull_request_files: dict[tuple[str, int], list[GitHubFile]] = {}
        self._commits: dict[tuple[str, str], GitHubCommit] = {}
        self._compares: dict[tuple[str, str, str], GitHubCompare] = {}
        self._issues: dict[tuple[str, int], GitHubIssue] = {}
        self._file_contents: dict[tuple[str, str], GitHubFileContents] = {}

        #: Every check run this client was asked to create, in creation order.
        self.check_runs: list[GitHubCheckRun] = []
        #: Every accepted check-run update, in order, as returned to the caller.
        self.check_run_updates: list[GitHubCheckRun] = []
        #: Every pull-request comment this client was asked to create, in order.
        self.comments: list[GitHubCommentRecord] = []

        self._next_check_run_id = 1
        self._next_comment_id = 1

    # -- Registration -----------------------------------------------------------------

    def register_repository(
        self,
        full_name: str,
        *,
        default_branch: str = "main",
        github_id: int | None = None,
        private: bool = False,
    ) -> GitHubRepository:
        """Register a repository and return its payload."""
        repository = GitHubRepository(
            id=github_id,
            full_name=full_name,
            name=full_name.rsplit("/", maxsplit=1)[-1],
            owner=GitHubUser(login=full_name.split("/", maxsplit=1)[0]),
            default_branch=default_branch,
            private=private,
            html_url=f"https://github.com/{full_name}",
        )
        self._repositories[full_name] = repository
        return repository

    def register_pull_request(
        self,
        full_name: str,
        number: int,
        *,
        title: str = "",
        body: str | None = None,
        author: str = "scout-demo",
        base_sha: str = DEFAULT_FAKE_BASE_SHA,
        head_sha: str = DEFAULT_FAKE_HEAD_SHA,
        base_ref: str = "main",
        head_ref: str = "scout/change",
        draft: bool = False,
        merged: bool = False,
        files: Sequence[GitHubFile | Mapping[str, Any]] | None = None,
    ) -> GitHubPullRequest:
        """Register a pull request, its SHAs, and optionally its changed files.

        The repository is auto-registered when absent, so a test can set up only the
        object it cares about without a second call.
        """
        if full_name not in self._repositories:
            self.register_repository(full_name)

        pull_request = GitHubPullRequest(
            number=_require_positive(number, field="pull request number"),
            title=title,
            body=body,
            html_url=f"https://github.com/{full_name}/pull/{number}",
            user=GitHubUser(login=author),
            base=GitHubBranchRef(ref=base_ref, sha=base_sha),
            head=GitHubBranchRef(ref=head_ref, sha=head_sha),
            draft=draft,
            merged=merged,
        )
        key = (full_name, number)
        self._pull_requests[key] = pull_request
        if files is not None:
            self._pull_request_files[key] = [_coerce_file(file) for file in files]
        return pull_request

    def register_pull_request_files(
        self,
        full_name: str,
        number: int,
        files: Sequence[GitHubFile | Mapping[str, Any]],
    ) -> list[GitHubFile]:
        """Register (or replace) the changed files of a pull request."""
        coerced = [_coerce_file(file) for file in files]
        self._pull_request_files[(full_name, number)] = coerced
        return coerced

    def register_commit(
        self,
        full_name: str,
        sha: str,
        *,
        message: str = "",
        author: str = "scout-demo",
    ) -> GitHubCommit:
        """Register a commit and its message."""
        commit = GitHubCommit(
            sha=sha,
            html_url=f"https://github.com/{full_name}/commit/{sha}",
            author=GitHubUser(login=author),
            commit=GitHubGitCommit(
                message=message,
                author=GitHubGitActor(name=author),
            ),
        )
        self._commits[(full_name, sha)] = commit
        return commit

    def register_compare(
        self,
        full_name: str,
        base: str,
        head: str,
        *,
        files: Sequence[GitHubFile | Mapping[str, Any]] = (),
        commits: Sequence[GitHubCommit] = (),
        status: str = "ahead",
    ) -> GitHubCompare:
        """Register the result of comparing ``base`` with ``head``."""
        compare = GitHubCompare(
            status=status,
            ahead_by=len(commits),
            total_commits=len(commits),
            commits=list(commits),
            files=[_coerce_file(file) for file in files],
        )
        self._compares[(full_name, base, head)] = compare
        return compare

    def register_issue(
        self,
        full_name: str,
        number: int,
        *,
        title: str = "",
        body: str | None = None,
        author: str = "scout-demo",
        is_pull_request: bool = False,
    ) -> GitHubIssue:
        """Register an issue (or a PR-as-issue, when ``is_pull_request``)."""
        issue = GitHubIssue(
            number=_require_positive(number, field="issue number"),
            title=title,
            body=body,
            html_url=f"https://github.com/{full_name}/issues/{number}",
            user=GitHubUser(login=author),
            pull_request={"url": "https://api.github.com/..."} if is_pull_request else None,
        )
        self._issues[(full_name, number)] = issue
        return issue

    def register_file_contents(
        self,
        full_name: str,
        path: str,
        *,
        text: str = "",
        ref: str | None = None,
    ) -> GitHubFileContents:
        """Register file contents, encoding ``text`` as GitHub would.

        The key includes ``ref`` so the same path can differ between branches, which is
        how a caller tests "read the file at the pinned SHA, not at main".
        """
        contents = GitHubFileContents(
            type="file",
            encoding="base64",
            size=len(text.encode("utf-8")),
            name=path.rsplit("/", maxsplit=1)[-1],
            path=path,
            sha="c" * 40,
            content=base64.b64encode(text.encode("utf-8")).decode("ascii"),
            html_url=f"https://github.com/{full_name}/blob/{ref or 'HEAD'}/{path}",
        )
        self._file_contents[(full_name, _content_key(path, ref))] = contents
        return contents

    # -- GitHubClient protocol --------------------------------------------------------

    async def get_repository(self, full_name: str) -> GitHubRepository:
        """Return a registered repository or raise :class:`GitHubNotFoundError`."""
        try:
            return self._repositories[full_name]
        except KeyError as exc:
            raise GitHubNotFoundError(
                f"no fake repository registered for {full_name!r}; call "
                "register_repository() first",
                full_name=full_name,
                registered=sorted(self._repositories),
            ) from exc

    async def get_pull_request(self, full_name: str, number: int) -> GitHubPullRequest:
        """Return a registered pull request or raise :class:`GitHubNotFoundError`."""
        try:
            return self._pull_requests[(full_name, number)]
        except KeyError as exc:
            raise GitHubNotFoundError(
                f"no fake pull request registered for {full_name}#{number}; call "
                "register_pull_request() first",
                full_name=full_name,
                number=number,
            ) from exc

    async def list_pull_request_files(self, full_name: str, number: int) -> list[GitHubFile]:
        """Return the registered files of a pull request (empty when none registered).

        The pull request itself must be registered, so a typo in the number surfaces as a
        not-found error rather than as a mysterious empty diff.
        """
        await self.get_pull_request(full_name, number)
        return list(self._pull_request_files.get((full_name, number), []))

    async def get_commit(self, full_name: str, sha: str) -> GitHubCommit:
        """Return a registered commit or raise :class:`GitHubNotFoundError`."""
        try:
            return self._commits[(full_name, sha)]
        except KeyError as exc:
            raise GitHubNotFoundError(
                f"no fake commit registered for {full_name}@{sha}; call register_commit() first",
                full_name=full_name,
                sha=sha,
            ) from exc

    async def compare_commits(self, full_name: str, base: str, head: str) -> GitHubCompare:
        """Return a registered comparison, or an empty one when ``base == head``.

        ``base == head`` legitimately yields an empty comparison (no commits ahead), so it
        is synthesised rather than requiring registration.
        """
        if base == head:
            return GitHubCompare(status="identical")
        try:
            return self._compares[(full_name, base, head)]
        except KeyError as exc:
            raise GitHubNotFoundError(
                f"no fake comparison registered for {full_name} {base}...{head}; call "
                "register_compare() first",
                full_name=full_name,
                base=base,
                head=head,
            ) from exc

    async def create_check_run(
        self,
        full_name: str,
        head_sha: str,
        name: str,
        status: CheckRunStatus | str = CheckRunStatus.COMPLETED,
        conclusion: CheckRunConclusion | str | None = None,
        title: str = "",
        summary: str = "",
        details_url: str | None = None,
        external_id: str | None = None,
    ) -> GitHubCheckRun:
        """Record a check run and return the payload GitHub would have returned.

        The payload is validated by the same
        :func:`~scout_github.checks.build_check_run_payload` the HTTP client uses, so a
        check run that GitHub would reject fails here too.
        """
        payload = build_check_run_payload(
            name=name,
            head_sha=head_sha,
            status=status,
            conclusion=conclusion,
            title=title,
            summary=summary,
            details_url=details_url,
            external_id=external_id,
        )
        check_run = GitHubCheckRun(
            id=self._next_check_run_id,
            name=name,
            head_sha=head_sha,
            status=str(payload["status"]),
            conclusion=str(payload["conclusion"]) if "conclusion" in payload else None,
            details_url=details_url
            or (f"https://scout.local/jobs/{external_id}" if external_id else None),
            external_id=external_id,
            html_url=f"https://github.com/{full_name}/runs/{self._next_check_run_id}",
            output=GitHubCheckOutput(title=title, summary=summary),
        )
        self._next_check_run_id += 1
        self.check_runs.append(check_run)
        return check_run

    async def update_check_run(
        self,
        full_name: str,
        check_run_id: int,
        *,
        status: CheckRunStatus | str | None = None,
        conclusion: CheckRunConclusion | str | None = None,
        title: str | None = None,
        summary: str | None = None,
        details_url: str | None = None,
        external_id: str | None = None,
    ) -> GitHubCheckRun:
        """Update a recorded check run, appending the result to ``check_run_updates``.

        ``check_runs`` keeps the creation payload and ``check_run_updates`` keeps the
        accepted updates, so a test can assert on either without one mutating the other.
        Fields not supplied keep their current value, mirroring the real PATCH endpoint
        so that a test cannot pass here while failing against GitHub.
        """
        existing = next(
            (run for run in self.check_runs if run.id == check_run_id),
            None,
        )
        if existing is None:
            raise GitHubNotFoundError(
                f"no fake check run with id {check_run_id}; create it first",
                full_name=full_name,
                check_run_id=check_run_id,
            )

        body = _build_check_run_update(
            status=status,
            conclusion=conclusion,
            title=title,
            summary=summary,
            details_url=details_url,
            external_id=external_id,
        )
        changes: dict[str, Any] = {}
        for field in ("status", "conclusion", "details_url", "external_id"):
            if field in body:
                changes[field] = body[field]

        output_update = body.get("output")
        if isinstance(output_update, Mapping):
            previous_title = existing.output.title if existing.output else None
            previous_summary = existing.output.summary if existing.output else None
            changes["output"] = GitHubCheckOutput(
                title=output_update.get("title", previous_title),
                summary=output_update.get("summary", previous_summary),
                text=existing.output.text if existing.output else None,
            )

        updated = existing.evolve(**changes)
        self.check_run_updates.append(updated)
        return updated

    async def create_pull_request_comment(
        self, full_name: str, number: int, body: str
    ) -> GitHubIssueComment:
        """Record a pull-request comment and return the payload GitHub would return."""
        comment = GitHubIssueComment(
            id=self._next_comment_id,
            body=body,
            html_url=(
                f"https://github.com/{full_name}/pull/{number}#issuecomment-{self._next_comment_id}"
            ),
            user=GitHubUser(login="scout-edrevel[bot]", type="Bot"),
        )
        self._next_comment_id += 1
        self.comments.append(
            GitHubCommentRecord(
                full_name=full_name,
                number=number,
                body=body,
                comment=comment,
            )
        )
        return comment

    async def get_issue(self, full_name: str, number: int) -> GitHubIssue:
        """Return a registered issue or raise :class:`GitHubNotFoundError`."""
        try:
            return self._issues[(full_name, number)]
        except KeyError as exc:
            raise GitHubNotFoundError(
                f"no fake issue registered for {full_name}#{number}; call register_issue() first",
                full_name=full_name,
                number=number,
            ) from exc

    async def get_file_contents(
        self, full_name: str, path: str, ref: str | None = None
    ) -> GitHubFileContents:
        """Return registered file contents or raise :class:`GitHubNotFoundError`."""
        try:
            return self._file_contents[(full_name, _content_key(path, ref))]
        except KeyError as exc:
            raise GitHubNotFoundError(
                f"no fake file contents registered for {full_name}:{path} at ref "
                f"{ref!r}; call register_file_contents() first",
                full_name=full_name,
                path=path,
                ref=ref,
            ) from exc


def _build_check_run_update(
    *,
    status: CheckRunStatus | str | None,
    conclusion: CheckRunConclusion | str | None,
    title: str | None,
    summary: str | None,
    details_url: str | None,
    external_id: str | None,
) -> dict[str, Any]:
    """Build a ``PATCH /check-runs/{id}`` body containing only the supplied fields.

    A PATCH must not clear fields the caller did not mention, so only non-``None``
    arguments are serialised. Validation is shared with creation through
    :func:`~scout_github.checks.resolve_check_state`.

    ``conclusion`` without ``status`` implies ``completed``: GitHub only accepts a
    conclusion on a completed run, and requiring callers to repeat the status would make
    the common "finish the run" call fail for no benefit.
    """
    body: dict[str, Any] = {}
    effective_status = status
    if effective_status is None and conclusion is not None:
        effective_status = CheckRunStatus.COMPLETED
    if effective_status is not None:
        resolved_status, resolved_conclusion = resolve_check_state(effective_status, conclusion)
        body["status"] = resolved_status.value
        if resolved_conclusion is not None:
            body["conclusion"] = resolved_conclusion.value

    output: dict[str, str] = {}
    if title is not None:
        output["title"] = title
    if summary is not None:
        output["summary"] = summary
    if output:
        body["output"] = output

    if details_url is not None:
        body["details_url"] = details_url
    if external_id is not None:
        body["external_id"] = external_id
    return body


def _coerce_file(file: GitHubFile | Mapping[str, Any]) -> GitHubFile:
    """Accept either a typed file entry or GitHub's raw JSON mapping."""
    if isinstance(file, GitHubFile):
        return file
    return GitHubFile.model_validate(file)


def _content_key(path: str, ref: str | None) -> str:
    """Key file contents by path and ref, so branches do not shadow one another."""
    return f"{ref or ''}:{path}"


def _require_positive(value: int, *, field: str) -> int:
    """Return ``value`` when it is a positive integer, else raise a typed error."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GitHubError(f"{field} must be a positive integer", field=field, value=value)
    return value


def _require_ref(value: str, *, field: str) -> str:
    """Return a non-empty ref or SHA with surrounding whitespace removed."""
    cleaned = value.strip()
    if not cleaned:
        raise GitHubError(f"{field} must not be empty")
    return cleaned


def _repo_path(full_name: str) -> str:
    """Validate ``owner/name`` and percent-encode each segment for a URL path."""
    parts = full_name.strip().split("/")
    if len(parts) != _REPO_FULL_NAME_SEGMENTS or not all(part.strip() for part in parts):
        raise GitHubError(
            f"invalid repository full name {full_name!r}; expected 'owner/name'",
            full_name=full_name,
        )
    return f"{quote(parts[0].strip(), safe='')}/{quote(parts[1].strip(), safe='')}"


def _parse_json(response: httpx.Response, *, resource: str) -> Any:
    """Decode a response body as JSON, raising a typed error when it is not JSON."""
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubError(
            f"GitHub returned a body that is not JSON for {resource}",
            status_code=response.status_code,
        ) from exc


def _parse_model[ModelT: BaseModel](
    model: type[ModelT],
    response: httpx.Response,
    *,
    resource: str,
) -> ModelT:
    """Validate a response body into ``model``, raising a typed error on mismatch."""
    payload = _parse_json(response, resource=resource)
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise GitHubError(
            f"GitHub returned an unexpected body for {resource}",
            errors=[
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:10]
            ],
        ) from exc


def _parse_records[ModelT: BaseModel](
    model: type[ModelT],
    payload: Any,
    *,
    resource: str,
) -> list[ModelT]:
    """Validate a JSON array body into a list of ``model``."""
    if not isinstance(payload, list):
        raise GitHubError(
            f"GitHub returned a non-array body for the list endpoint {resource}",
            resource=resource,
        )
    records: list[ModelT] = []
    for item in payload:
        try:
            records.append(model.model_validate(item))
        except ValidationError as exc:
            raise GitHubError(
                f"GitHub returned an unexpected array element for {resource}",
                errors=[
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()[:10]
                ],
            ) from exc
    return records
