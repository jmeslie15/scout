"""Delivery channel adapters (spec section 47)."""

from __future__ import annotations

from scout_notifications.channels.email import EmailChannelAdapter, SmtpSettings
from scout_notifications.channels.github import (
    GitHubChannelAdapter,
    GitHubCommentClient,
)
from scout_notifications.channels.google_chat import GoogleChatChannelAdapter

__all__ = [
    "EmailChannelAdapter",
    "GitHubChannelAdapter",
    "GitHubCommentClient",
    "GoogleChatChannelAdapter",
    "SmtpSettings",
]
