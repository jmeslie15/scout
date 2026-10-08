"""``HttpGitHubClient``: headers, resource calls, pagination, and error mapping.

Every request in this module is served from a respx route. ``respx.mock`` asserts that all
requests are mocked by default, so an accidental real call fails the test instead of
reaching GitHub.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx

from scout_github.checks import CheckRunConclusion, CheckRunStatus
from scout_github.client import HttpGitHubClient
from scout_github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTransientError,
)

API = "https://api.github.test"
BASE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"

REPO_JSON: dict[str, Any] = {
    "id": 741852963,
    "full_name": "edrevel/scout-demo",
    "default_branch": "main",
    "private": True,
    "html_url": "https://github.com/edrevel/scout-demo",
}

PR_JSON: dict[str, Any] = {
    "number": 42,
    "title": "Add acceptance-criteria verification",
    "body": None,
    "draft": False,
    "merged": False,
    "base": {"ref": "main", "sha": BASE_SHA},
    "head": {"ref": "feature/verification", "sha": HEAD_SHA},
}

FILE_JSON: dict[str, Any] = {
    "filename": "src/service.py",
    "status": "modified",
    "additions": 4,
    "deletions": 2,
    "changes": 6,
    "patch": "@@ -1 +1 @@\n-old\n+new\n",
}

CHECK_RUN_JSON: dict[str, Any] = {
    "id": 555,
    "name": "Scout Verification",
    "head_sha": HEAD_SHA,
    "status": "completed",
    "conclusion": "success",
    "html_url": "https://github.com/edrevel/scout-demo/runs/555",
}


def _json_body(request: httpx.Request) -> Any:
    """Decode the JSON request body; ``httpx.Request`` exposes bytes, not a JSON view."""
    return json.loads(request.content)


def _client(**overrides: Any) -> HttpGitHubClient:
    kwargs: dict[str, Any] = {
        "installation_id": 42,
        "tokens": "ghs_test_token",
        "base_url": API,
    }
    kwargs.update(overrides)
    return HttpGitHubClient(**kwargs)


class _RecordingTokenProvider:
    """A token provider that records which installations were asked for a token."""

    def __init__(self, token: str = "ghs_from_provider") -> None:
        self.token = token
        self.requested: list[int] = []

    async def get_installation_token(self, installation_id: int) -> str:
        self.requested.append(installation_id)
        return self.token


# -- Headers and construction --------------------------------------------------------


@respx.mock
async def test_requests_carry_the_required_headers_and_token() -> None:
    route = respx.get(f"{API}/repos/edrevel/scout-demo").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    client = _client()

    repository = await client.get_repository("edrevel/scout-demo")

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer ghs_test_token"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert request.headers["User-Agent"] == "scout-edrevel"
    assert repository.full_name == "edrevel/scout-demo"
    assert repository.id == 741852963

    await client.aclose()


@respx.mock
async def test_a_token_provider_is_used_for_the_installation() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    provider = _RecordingTokenProvider()
    client = _client(tokens=provider)

    await client.get_repository("edrevel/scout-demo")

    assert provider.requested == [42]
    await client.aclose()


async def test_constructor_rejects_a_non_positive_installation_id() -> None:
    with pytest.raises(GitHubError, match="installation_id"):
        _client(installation_id=0)


async def test_named_repository_without_a_slash_is_rejected_before_any_request() -> None:
    client = _client()

    with pytest.raises(GitHubError, match="expected 'owner/name'"):
        await client.get_repository("not-a-full-name")

    await client.aclose()


@respx.mock
async def test_repository_path_segments_are_percent_encoded() -> None:
    route = respx.get(url__regex=r"https://api\.github\.test/repos/.*").mock(
        return_value=httpx.Response(200, json=REPO_JSON)
    )
    client = _client()

    await client.get_repository("edrevel/scout demo")

    assert "%20" in str(route.calls[0].request.url)
    await client.aclose()


async def test_injected_http_client_is_not_closed_by_aclose() -> None:
    injected = httpx.AsyncClient()
    client = _client(http_client=injected)

    await client.aclose()

    assert injected.is_closed is False
    await injected.aclose()


# -- Resource calls ------------------------------------------------------------------


@respx.mock
async def test_get_pull_request_returns_pinned_shas() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo/pulls/42").mock(
        return_value=httpx.Response(200, json=PR_JSON)
    )
    client = _client()

    pull_request = await client.get_pull_request("edrevel/scout-demo", 42)

    assert pull_request.head.sha == HEAD_SHA
    assert pull_request.base.sha == BASE_SHA
    assert pull_request.body is None
    await client.aclose()


@respx.mock
async def test_get_commit_reads_the_message() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo/commits/{HEAD_SHA}").mock(
        return_value=httpx.Response(
            200, json={"sha": HEAD_SHA, "commit": {"message": "Fix the classifier"}}
        )
    )
    client = _client()

    commit = await client.get_commit("edrevel/scout-demo", HEAD_SHA)

    assert commit.message == "Fix the classifier"
    await client.aclose()


@respx.mock
async def test_compare_commits_keeps_the_three_dot_range() -> None:
    route = respx.get(f"{API}/repos/edrevel/scout-demo/compare/{BASE_SHA}...{HEAD_SHA}").mock(
        return_value=httpx.Response(
            200, json={"status": "ahead", "total_commits": 1, "files": [FILE_JSON]}
        )
    )
    client = _client()

    comparison = await client.compare_commits("edrevel/scout-demo", BASE_SHA, HEAD_SHA)

    assert comparison.total_commits == 1
    assert comparison.files[0].filename == "src/service.py"
    assert route.called
    await client.aclose()


@respx.mock
async def test_compare_commits_accepts_branch_refs_with_slashes() -> None:
    route = respx.get(f"{API}/repos/edrevel/scout-demo/compare/main...feature/thing").mock(
        return_value=httpx.Response(200, json={"status": "ahead", "files": []})
    )
    client = _client()

    await client.compare_commits("edrevel/scout-demo", "main", "feature/thing")

    assert "/compare/main...feature/thing" in str(route.calls[0].request.url)
    await client.aclose()


@respx.mock
async def test_get_issue_returns_the_linked_issue_body() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo/issues/7").mock(
        return_value=httpx.Response(
            200, json={"number": 7, "title": "Requirement", "body": "Must do X"}
        )
    )
    client = _client()

    issue = await client.get_issue("edrevel/scout-demo", 7)

    assert issue.body == "Must do X"
    await client.aclose()


@respx.mock
async def test_get_file_contents_decodes_base64_with_newlines() -> None:
    encoded = base64.b64encode(b"line one\nline two\n").decode("ascii")
    wrapped = f"{encoded[:8]}\n{encoded[8:]}"
    route = respx.get(f"{API}/repos/edrevel/scout-demo/contents/src/service.py").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": "base64",
                "path": "src/service.py",
                "content": wrapped,
                "size": 19,
            },
        )
    )
    client = _client()

    contents = await client.get_file_contents("edrevel/scout-demo", "src/service.py", ref=HEAD_SHA)

    assert contents.decoded_text() == "line one\nline two\n"
    assert route.calls[0].request.url.params["ref"] == HEAD_SHA
    await client.aclose()


@respx.mock
async def test_directory_listing_from_the_contents_api_is_rejected() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo/contents/src").mock(
        return_value=httpx.Response(200, json=[{"name": "service.py", "type": "file"}])
    )
    client = _client()

    with pytest.raises(GitHubError, match="is a directory"):
        await client.get_file_contents("edrevel/scout-demo", "src")

    await client.aclose()


@respx.mock
async def test_non_json_body_is_reported_as_a_github_error() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo").mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    client = _client()

    with pytest.raises(GitHubError, match="not JSON"):
        await client.get_repository("edrevel/scout-demo")

    await client.aclose()


@respx.mock
async def test_unexpected_body_shape_is_reported_as_a_github_error() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo").mock(
        return_value=httpx.Response(200, json={"unexpected": True})
    )
    client = _client()

    with pytest.raises(GitHubError, match="unexpected body"):
        await client.get_repository("edrevel/scout-demo")

    await client.aclose()


# -- Pagination ----------------------------------------------------------------------


@respx.mock
async def test_pull_request_files_follow_the_next_link() -> None:
    second_page = f"{API}/repos/edrevel/scout-demo/pulls/42/files?per_page=100&page=2"
    requested_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        requested_pages.append(page)
        if page == "2":
            return httpx.Response(
                200,
                json=[
                    {**FILE_JSON, "filename": "b.py"},
                    {**FILE_JSON, "filename": "c.py"},
                ],
            )
        return httpx.Response(
            200,
            json=[FILE_JSON],
            headers={"Link": f'<{second_page}>; rel="next"'},
        )

    route = respx.get(f"{API}/repos/edrevel/scout-demo/pulls/42/files").mock(side_effect=handler)
    client = _client()

    files = await client.list_pull_request_files("edrevel/scout-demo", 42)

    assert [file.filename for file in files] == ["src/service.py", "b.py", "c.py"]
    assert route.call_count == 2
    assert requested_pages == [None, "2"]
    await client.aclose()


@respx.mock
async def test_pagination_is_capped_and_the_cap_is_logged() -> None:
    second_page = f"{API}/repos/edrevel/scout-demo/pulls/42/files?per_page=100&page=2"
    respx.get(f"{API}/repos/edrevel/scout-demo/pulls/42/files").mock(
        return_value=httpx.Response(
            200,
            json=[FILE_JSON],
            headers={"Link": f'<{second_page}>; rel="next"'},
        )
    )
    client = _client(max_pages=1)

    files = await client.list_pull_request_files("edrevel/scout-demo", 42)

    assert [file.filename for file in files] == ["src/service.py"]
    await client.aclose()


# -- Check runs and comments ---------------------------------------------------------


@respx.mock
async def test_create_check_run_posts_the_expected_body() -> None:
    route = respx.post(f"{API}/repos/edrevel/scout-demo/check-runs").mock(
        return_value=httpx.Response(201, json=CHECK_RUN_JSON)
    )
    client = _client()

    check_run = await client.create_check_run(
        "edrevel/scout-demo",
        HEAD_SHA,
        "Scout Verification",
        status=CheckRunStatus.COMPLETED,
        conclusion=CheckRunConclusion.SUCCESS,
        title="Scout verified this change",
        summary="All checks passed.",
        details_url="http://localhost:5173/jobs/job_1",
        external_id="job_1",
    )

    body = _json_body(route.calls[0].request)
    assert body == {
        "name": "Scout Verification",
        "head_sha": HEAD_SHA,
        "status": "completed",
        "conclusion": "success",
        "output": {"title": "Scout verified this change", "summary": "All checks passed."},
        "details_url": "http://localhost:5173/jobs/job_1",
        "external_id": "job_1",
    }
    assert check_run.id == 555
    assert check_run.conclusion == "success"
    await client.aclose()


@respx.mock
async def test_create_check_run_omits_optional_fields_when_not_supplied() -> None:
    route = respx.post(f"{API}/repos/edrevel/scout-demo/check-runs").mock(
        return_value=httpx.Response(201, json=CHECK_RUN_JSON)
    )
    client = _client()

    await client.create_check_run(
        "edrevel/scout-demo",
        HEAD_SHA,
        "Scout Verification",
        conclusion=CheckRunConclusion.NEUTRAL,
        title="Human review required",
        summary="Scout needs a human decision.",
    )

    body = _json_body(route.calls[0].request)
    assert "details_url" not in body
    assert "external_id" not in body
    await client.aclose()


@respx.mock
async def test_create_check_run_rejects_an_invalid_state_locally() -> None:
    respx.post(f"{API}/repos/edrevel/scout-demo/check-runs").mock(
        return_value=httpx.Response(201, json=CHECK_RUN_JSON)
    )
    client = _client()

    with pytest.raises(ValueError, match="requires a conclusion"):
        await client.create_check_run(
            "edrevel/scout-demo",
            HEAD_SHA,
            "Scout Verification",
            status=CheckRunStatus.COMPLETED,
        )

    await client.aclose()


@respx.mock
async def test_update_check_run_patches_only_supplied_fields() -> None:
    route = respx.patch(f"{API}/repos/edrevel/scout-demo/check-runs/555").mock(
        return_value=httpx.Response(200, json={**CHECK_RUN_JSON, "conclusion": "neutral"})
    )
    client = _client()

    updated = await client.update_check_run(
        "edrevel/scout-demo",
        555,
        status=CheckRunStatus.COMPLETED,
        conclusion=CheckRunConclusion.NEUTRAL,
        summary="Needs a human decision.",
    )

    body = _json_body(route.calls[0].request)
    assert body == {
        "status": "completed",
        "conclusion": "neutral",
        "output": {"summary": "Needs a human decision."},
    }
    assert updated.conclusion == "neutral"
    await client.aclose()


@respx.mock
async def test_update_check_run_can_change_only_the_output() -> None:
    """A PATCH that mentions only the summary must not complete or conclude the run."""
    route = respx.patch(f"{API}/repos/edrevel/scout-demo/check-runs/555").mock(
        return_value=httpx.Response(200, json=CHECK_RUN_JSON)
    )
    client = _client()

    await client.update_check_run(
        "edrevel/scout-demo",
        555,
        summary="Still running static analysis.",
    )

    assert _json_body(route.calls[0].request) == {
        "output": {"summary": "Still running static analysis."}
    }
    await client.aclose()


@respx.mock
async def test_update_check_run_infers_completion_from_a_conclusion() -> None:
    route = respx.patch(f"{API}/repos/edrevel/scout-demo/check-runs/555").mock(
        return_value=httpx.Response(200, json=CHECK_RUN_JSON)
    )
    client = _client()

    await client.update_check_run("edrevel/scout-demo", 555, conclusion=CheckRunConclusion.SUCCESS)

    assert _json_body(route.calls[0].request) == {
        "status": "completed",
        "conclusion": "success",
    }
    await client.aclose()


@respx.mock
async def test_update_check_run_rejects_a_conclusion_on_an_unfinished_run() -> None:
    client = _client()

    with pytest.raises(ValueError, match="cannot carry a conclusion"):
        await client.update_check_run(
            "edrevel/scout-demo",
            555,
            status=CheckRunStatus.IN_PROGRESS,
            conclusion=CheckRunConclusion.SUCCESS,
        )

    await client.aclose()


@respx.mock
async def test_update_check_run_rejects_a_non_positive_id() -> None:
    client = _client()

    with pytest.raises(GitHubError, match="check run id"):
        await client.update_check_run("edrevel/scout-demo", 0)

    await client.aclose()


@respx.mock
async def test_create_pull_request_comment_posts_the_body() -> None:
    route = respx.post(f"{API}/repos/edrevel/scout-demo/issues/42/comments").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": 9001,
                "body": "Which retry policy applies here?",
                "html_url": "https://github.com/edrevel/scout-demo/pull/42#issuecomment-9001",
            },
        )
    )
    client = _client()

    comment = await client.create_pull_request_comment(
        "edrevel/scout-demo", 42, "Which retry policy applies here?"
    )

    assert _json_body(route.calls[0].request) == {"body": "Which retry policy applies here?"}
    assert comment.id == 9001
    await client.aclose()


# -- Error mapping -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, GitHubAuthError),
        (403, GitHubAuthError),
        (404, GitHubNotFoundError),
        (500, GitHubTransientError),
        (503, GitHubTransientError),
    ],
)
@respx.mock
async def test_http_failures_map_onto_typed_errors(status: int, expected: type[Exception]) -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo").mock(
        return_value=httpx.Response(status, json={"message": "nope"})
    )
    client = _client()

    with pytest.raises(expected):
        await client.get_repository("edrevel/scout-demo")

    await client.aclose()


@respx.mock
async def test_rate_limit_carries_the_retry_after_delay() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "30"},
            json={"message": "API rate limit exceeded"},
        )
    )
    client = _client()

    with pytest.raises(GitHubRateLimitError) as excinfo:
        await client.get_repository("edrevel/scout-demo")

    assert excinfo.value.retry_after_seconds == 30.0
    await client.aclose()


@respx.mock
async def test_forbidden_with_an_exhausted_quota_is_a_rate_limit() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo").mock(
        return_value=httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4102444800"},
            json={"message": "API rate limit exceeded"},
        )
    )
    client = _client()

    with pytest.raises(GitHubRateLimitError) as excinfo:
        await client.get_repository("edrevel/scout-demo")

    assert excinfo.value.retry_after_seconds is not None
    await client.aclose()


@respx.mock
async def test_transport_failures_are_transient() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    client = _client()

    with pytest.raises(GitHubTransientError, match="ConnectError"):
        await client.get_repository("edrevel/scout-demo")

    await client.aclose()


@respx.mock
async def test_timeouts_are_transient() -> None:
    respx.get(f"{API}/repos/edrevel/scout-demo").mock(side_effect=httpx.ReadTimeout("too slow"))
    client = _client()

    with pytest.raises(GitHubTransientError, match="ReadTimeout"):
        await client.get_repository("edrevel/scout-demo")

    await client.aclose()


@respx.mock
async def test_the_client_never_retries() -> None:
    """Retry policy belongs to the orchestrator (spec section 51)."""
    route = respx.get(f"{API}/repos/edrevel/scout-demo").mock(
        return_value=httpx.Response(503, json={"message": "Service Unavailable"})
    )
    client = _client()

    with pytest.raises(GitHubTransientError):
        await client.get_repository("edrevel/scout-demo")

    assert route.call_count == 1
    await client.aclose()
