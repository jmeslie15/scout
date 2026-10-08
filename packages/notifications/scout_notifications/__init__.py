"""Severity-routed notifications with templating, deduplication, and pluggable channels.

Spec sections 47 and 72. The package is offline by default: with
``SCOUT_NOTIFICATIONS_ENABLED=false`` (the documented default) every event is suppressed
with a recorded reason, and nothing reaches the network.
"""

from __future__ import annotations

from scout_notifications.base import (
    DeliveryResult,
    NotificationChannelAdapter,
    NotificationMessage,
)
from scout_notifications.channels import (
    EmailChannelAdapter,
    GitHubChannelAdapter,
    GitHubCommentClient,
    GoogleChatChannelAdapter,
    SmtpSettings,
)
from scout_notifications.routing import (
    DEFAULT_ROUTING,
    NOTIFICATIONS_ENABLED_ENV,
    NotificationEventKind,
    RoutingDecision,
    RoutingPolicy,
    dedup_key_for,
    email_recipients_for,
    notifications_enabled_from_env,
)
from scout_notifications.service import (
    DEFAULT_DEDUP_WINDOW_SECONDS,
    InMemoryNotificationHistory,
    NotificationHistory,
    NotificationService,
    default_job_url,
    severity_for_report,
)
from scout_notifications.templating import TemplateRenderer, build_environment

__all__ = [
    "DEFAULT_DEDUP_WINDOW_SECONDS",
    "DEFAULT_ROUTING",
    "NOTIFICATIONS_ENABLED_ENV",
    "DeliveryResult",
    "EmailChannelAdapter",
    "GitHubChannelAdapter",
    "GitHubCommentClient",
    "GoogleChatChannelAdapter",
    "InMemoryNotificationHistory",
    "NotificationChannelAdapter",
    "NotificationEventKind",
    "NotificationHistory",
    "NotificationMessage",
    "NotificationService",
    "RoutingDecision",
    "RoutingPolicy",
    "SmtpSettings",
    "TemplateRenderer",
    "build_environment",
    "dedup_key_for",
    "default_job_url",
    "email_recipients_for",
    "notifications_enabled_from_env",
    "severity_for_report",
]
