"""GitHub comment adapter: protocol delegation without importing ``scout_github``."""

from __future__ import annotations

import pytest

from scout_domain.enums import NotificationChannel, Severity
from scout_notifications.base import NotificationMessage
from scout_notifications.channels.github import (
    GITHUB_PR_NUMBER_METADATA_KEY,
    GITHUB_REPOSITORY_METADATA_KEY,
    GitHubChannelAdapter,
    GitHubCommentClient,
)


class FakeGitHubClient:
    """Structurally satisfies :class:`GitHubCommentClient`."""

    def __init__(self, *, comment_id: str = "comment-1", raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, int, str]] = []
        self._comment_id = comment_id
        self._raises = raises

    async def create_pull_request_comment(
        self,
        full_name: str,
        number: int,
        body: str,
    ) -> str:
        self.calls.append((full_name, number, body))
        if self._raises is not None:
            raise self._raises
        return self._comment_id


@pytest.fixture
def message() -> NotificationMessage:
    return NotificationMessage(
        subject="Question: export metadata",
        body="Should managers see export metadata?",
        severity=Severity.HIGH,
        job_url="https://scout.test/jobs/job_1",
        metadata={
            GITHUB_REPOSITORY_METADATA_KEY: "acme/api",
            GITHUB_PR_NUMBER_METADATA_KEY: "42",
        },
    )


def test_fake_client_satisfies_the_protocol() -> None:
    assert isinstance(FakeGitHubClient(), GitHubCommentClient)


async def test_send_delegates_to_the_injected_client(message: NotificationMessage) -> None:
    client = FakeGitHubClient(comment_id="comment-99")
    adapter = GitHubChannelAdapter(client)

    result = await adapter.send(message)

    assert result.delivered is True
    assert result.provider_message_id == "comment-99"
    assert client.calls == [("acme/api", 42, message.body)]


async def test_missing_metadata_is_a_non_retryable_failure() -> None:
    client = FakeGitHubClient()
    adapter = GitHubChannelAdapter(client)
    incomplete = NotificationMessage(
        subject="Subject",
        body="Body",
        severity=Severity.HIGH,
    )

    result = await adapter.send(incomplete)

    assert result.delivered is False
    assert result.retryable is False
    assert result.error is not None
    assert GITHUB_REPOSITORY_METADATA_KEY in result.error
    assert client.calls == []


async def test_non_integer_pull_request_number_is_rejected(message: NotificationMessage) -> None:
    client = FakeGitHubClient()
    adapter = GitHubChannelAdapter(client)
    broken = message.evolve(
        metadata={
            GITHUB_REPOSITORY_METADATA_KEY: "acme/api",
            GITHUB_PR_NUMBER_METADATA_KEY: "not-a-number",
        }
    )

    result = await adapter.send(broken)

    assert result.delivered is False
    assert result.retryable is False
    assert result.error is not None
    assert "pull_request_number" in result.error
    assert client.calls == []


async def test_client_failure_is_recorded_and_not_retried(message: NotificationMessage) -> None:
    client = FakeGitHubClient(raises=RuntimeError("github is down"))
    adapter = GitHubChannelAdapter(client)

    result = await adapter.send(message)

    assert result.delivered is False
    assert result.retryable is False
    assert result.error is not None
    assert "RuntimeError" in result.error


def test_supports_only_its_own_channel() -> None:
    adapter = GitHubChannelAdapter(FakeGitHubClient())

    assert adapter.name is NotificationChannel.GITHUB
    assert adapter.supports(NotificationChannel.GITHUB) is True
    assert adapter.supports(NotificationChannel.GOOGLE_CHAT) is False


async def test_adapter_does_not_import_scout_github() -> None:
    """The package must not acquire a cross-package dependency it does not own."""
    from pathlib import Path

    import scout_notifications.channels.github as github_module

    source = Path(str(github_module.__file__)).read_text(encoding="utf-8")
    statements = [line.strip() for line in source.splitlines()]
    assert not any(
        line.startswith(("import scout_github", "from scout_github")) for line in statements
    )
