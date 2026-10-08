"""GitHub pull-request comment adapter (spec section 47).

This adapter deliberately does not import ``scout_github``. Two reasons:

* Rule 2 — the integration boundary is a protocol, so any object shaped like
  :class:`GitHubCommentClient` works, including a fake in tests and the real client in the
  worker.
* layering — ``scout_notifications`` does not depend on ``scout_github`` in the workspace
  dependency graph, and adding a cross-package dependency to post a comment would invert
  that.

The repository and pull-request number arrive in ``NotificationMessage.metadata`` rather
than in the message signature, because routing already knows the job and the message is
the only channel-agnostic carrier.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from scout_domain.enums import NotificationChannel
from scout_notifications.base import DeliveryResult, NotificationMessage
from scout_notifications.observability import get_logger

__all__ = [
    "GITHUB_PR_NUMBER_METADATA_KEY",
    "GITHUB_REPOSITORY_METADATA_KEY",
    "GitHubChannelAdapter",
    "GitHubCommentClient",
]

logger = get_logger(__name__)

#: Metadata keys this adapter requires. Documented as constants so the service and the
#: adapter cannot drift on spelling.
GITHUB_REPOSITORY_METADATA_KEY: Final[str] = "repository_full_name"
GITHUB_PR_NUMBER_METADATA_KEY: Final[str] = "pull_request_number"


@runtime_checkable
class GitHubCommentClient(Protocol):
    """The narrow slice of a GitHub client this adapter uses."""

    async def create_pull_request_comment(
        self,
        full_name: str,
        number: int,
        body: str,
    ) -> str:
        """Create a comment on a pull request and return its identifier or URL."""
        ...


class GitHubChannelAdapter:
    """Deliver notifications as pull-request comments via an injected client."""

    name: NotificationChannel = NotificationChannel.GITHUB

    def __init__(self, client: GitHubCommentClient) -> None:
        self._client = client

    def supports(self, channel: NotificationChannel) -> bool:
        """Whether this adapter serves ``channel``."""
        return channel is self.name

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """Post ``message.body`` as a comment on the pull request it refers to."""
        full_name = message.metadata.get(GITHUB_REPOSITORY_METADATA_KEY, "").strip()
        raw_number = message.metadata.get(GITHUB_PR_NUMBER_METADATA_KEY, "").strip()
        if not full_name or not raw_number:
            return DeliveryResult.failure(
                "github delivery requires repository_full_name and pull_request_number "
                "in the message metadata",
                retryable=False,
            )
        try:
            number = int(raw_number)
        except ValueError:
            return DeliveryResult.failure(
                f"pull_request_number is not an integer: {raw_number!r}",
                retryable=False,
            )

        try:
            comment_id = await self._client.create_pull_request_comment(
                full_name,
                number,
                message.body,
            )
        except Exception as exc:
            # The client is an external integration; an unexpected exception type must be
            # recorded rather than propagated (Rule 12), and an unclassified failure is
            # treated as non-retryable, which is the fail-closed default.
            logger.warning(
                "github comment failed",
                extra={"severity": message.severity.value, "error": type(exc).__name__},
            )
            return DeliveryResult.failure(
                f"github comment failed ({type(exc).__name__})",
                retryable=False,
            )

        return DeliveryResult.success(comment_id or None)
