"""Contract: both GitHub client implementations satisfy the published interface.

``GitHubClient`` is the boundary the rest of the platform programs against (Rule 2). If an
implementation drifts — a renamed parameter, a dropped method, a synchronous method where
the protocol promises a coroutine — the failure must appear here rather than three layers
away in the orchestrator.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

import scout_github
from scout_github import client as client_module
from scout_github.client import FakeGitHubClient, GitHubClient, HttpGitHubClient

API = "https://api.github.test"

EXPECTED_METHODS = {
    "get_repository",
    "get_pull_request",
    "list_pull_request_files",
    "get_commit",
    "compare_commits",
    "create_check_run",
    "update_check_run",
    "create_pull_request_comment",
    "get_issue",
    "get_file_contents",
}


def _protocol_methods() -> dict[str, Any]:
    """The callables ``GitHubClient`` publishes, discovered from the protocol itself."""
    return {
        name: getattr(GitHubClient, name)
        for name in dir(GitHubClient)
        if not name.startswith("_") and callable(getattr(GitHubClient, name))
    }


def _http_client() -> HttpGitHubClient:
    return HttpGitHubClient(installation_id=42, tokens="ghs_test_token", base_url=API)


def test_protocol_publishes_exactly_the_expected_interface() -> None:
    assert set(_protocol_methods()) == EXPECTED_METHODS


def test_fake_client_is_a_runtime_instance_of_the_protocol() -> None:
    assert isinstance(FakeGitHubClient(), GitHubClient)


async def test_http_client_is_a_runtime_instance_of_the_protocol() -> None:
    http_client = _http_client()

    assert isinstance(http_client, GitHubClient)

    await http_client.aclose()


def test_partial_implementations_are_not_instances() -> None:
    class OnlyOneMethod:
        async def get_repository(self, full_name: str) -> None: ...

    assert isinstance(OnlyOneMethod(), GitHubClient) is False
    assert isinstance(object(), GitHubClient) is False


@pytest.mark.parametrize("method_name", sorted(EXPECTED_METHODS))
def test_both_implementations_are_coroutines(method_name: str) -> None:
    assert inspect.iscoroutinefunction(getattr(GitHubClient, method_name))
    assert inspect.iscoroutinefunction(getattr(HttpGitHubClient, method_name))
    assert inspect.iscoroutinefunction(getattr(FakeGitHubClient, method_name))


@pytest.mark.parametrize("method_name", sorted(EXPECTED_METHODS))
def test_both_implementations_match_the_protocol_signature(method_name: str) -> None:
    expected = _parameters(getattr(GitHubClient, method_name))

    assert _parameters(getattr(HttpGitHubClient, method_name)) == expected
    assert _parameters(getattr(FakeGitHubClient, method_name)) == expected


def test_protocol_is_exported_from_the_package_root() -> None:
    """Callers import the interface from the package, not from a submodule path."""
    assert scout_github.GitHubClient is GitHubClient
    assert client_module.GitHubClient is GitHubClient


def _parameters(method: Any) -> list[tuple[str, int]]:
    """Parameter names and kinds, excluding ``self``, as a comparable list."""
    return [
        (name, parameter.kind)
        for name, parameter in inspect.signature(method).parameters.items()
        if name != "self"
    ]
