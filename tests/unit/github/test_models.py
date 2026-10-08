"""GitHub payload models: tolerant parsing and content decoding."""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from scout_github.errors import GitHubError
from scout_github.models import (
    GitHubCheckRun,
    GitHubCommit,
    GitHubFile,
    GitHubFileContents,
    GitHubIssue,
    GitHubPullRequest,
    GitHubRepository,
)

BASE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"


def test_unknown_fields_are_ignored() -> None:
    """GitHub adds fields regularly; Scout must not break on them."""
    repository = GitHubRepository.model_validate(
        {
            "id": 1,
            "full_name": "edrevel/scout-demo",
            "default_branch": "main",
            "some_future_field": {"nested": True},
            "auto_merge": None,
        }
    )

    assert repository.full_name == "edrevel/scout-demo"
    assert not hasattr(repository, "some_future_field")


def test_partial_payloads_parse_with_defaults() -> None:
    pull_request = GitHubPullRequest.model_validate({"number": 7})

    assert pull_request.number == 7
    assert pull_request.base.sha == ""
    assert pull_request.draft is False
    assert pull_request.body is None


def test_commit_message_is_flattened_from_the_git_object() -> None:
    commit = GitHubCommit.model_validate(
        {"sha": HEAD_SHA, "commit": {"message": "Fix the retry classifier", "author": {}}}
    )

    assert commit.message == "Fix the retry classifier"


def test_missing_nested_git_object_yields_an_empty_message() -> None:
    assert GitHubCommit.model_validate({"sha": HEAD_SHA}).message == ""


def test_evolve_returns_a_validated_copy() -> None:
    file = GitHubFile.model_validate(
        {"filename": "a.py", "status": "modified", "additions": 1, "patch": "@@\n"}
    )

    evolved = file.evolve(status="added", additions=9)

    assert evolved.status == "added"
    assert evolved.additions == 9
    assert file.status == "modified"
    assert evolved.filename == file.filename


def test_check_run_requires_an_id() -> None:
    with pytest.raises(ValidationError, match="id"):
        GitHubCheckRun.model_validate({"name": "Scout Verification"})


def test_issue_body_and_pull_request_marker_are_optional() -> None:
    issue = GitHubIssue.model_validate({"number": 3})

    assert issue.body is None
    assert issue.pull_request is None


# -- File contents -------------------------------------------------------------------


def _contents(text: bytes) -> GitHubFileContents:
    encoded = base64.b64encode(text).decode("ascii")
    # GitHub returns base64 with embedded newlines; decoded_text must tolerate them.
    wrapped = f"{encoded[:8]}\n{encoded[8:]}" if len(encoded) > 8 else encoded
    return GitHubFileContents.model_validate(
        {
            "type": "file",
            "encoding": "base64",
            "path": "src/service.py",
            "size": len(text),
            "content": wrapped,
        }
    )


def test_decoded_bytes_handles_githubs_embedded_newlines() -> None:
    assert _contents(b"line one\nline two\n").decoded_bytes() == b"line one\nline two\n"


def test_decoded_text_round_trips_utf8() -> None:
    assert _contents("café ☕\n".encode()).decoded_text() == "café ☕\n"


def test_invalid_base64_is_reported_as_a_github_error() -> None:
    contents = GitHubFileContents.model_validate({"path": "x", "content": "abc"})

    with pytest.raises(GitHubError, match="not valid base64"):
        contents.decoded_bytes()


def test_binary_content_is_reported_when_decoded_as_text() -> None:
    contents = _contents(b"\xff\xfe\x00\x01")

    with pytest.raises(GitHubError, match="probably binary"):
        contents.decoded_text()


def test_decoded_text_accepts_an_explicit_encoding() -> None:
    assert _contents("héllo".encode("latin-1")).decoded_text("latin-1") == "héllo"
