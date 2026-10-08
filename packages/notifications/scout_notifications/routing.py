"""Severity-based routing and anti-spam (spec section 47).

The default routing table is deliberately quiet at the bottom::

    INFO     -> {}                                  nothing
    LOW      -> {}                                  nothing
    MEDIUM   -> {GITHUB}
    HIGH     -> {GITHUB, GOOGLE_CHAT}
    CRITICAL -> {GITHUB, GOOGLE_CHAT, EMAIL}

This is a product decision, not an oversight. Notification spam destroys trust in the
system faster than missing information does: once people mute Scout, a CRITICAL alert
reaches nobody. Information-severity events stay in the dashboard and the report, where
someone who wants them can look; only MEDIUM and above interrupt anyone.

Two further mechanisms keep volume honest:

* a stable ``dedup_key`` per underlying fact, so the same question is not announced twice;
* per-repository overrides (``min_severity``, ``quiet_on_success``), so a noisy repository
  can be quieted without changing the global policy.

The whole feature is behind ``SCOUT_NOTIFICATIONS_ENABLED`` (default ``false``), because a
fresh checkout must not try to reach the network.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from scout_domain.config import NotificationsConfig, ScoutRepositoryConfig
from scout_domain.enums import NotificationChannel, Severity
from scout_domain.models.finding import Finding
from scout_domain.models.job import VerificationJob
from scout_domain.models.question import Question
from scout_domain.models.report import VerificationReport

__all__ = [
    "DEFAULT_ROUTING",
    "NOTIFICATIONS_ENABLED_ENV",
    "SUPPRESSED_DISABLED",
    "SUPPRESSED_DUPLICATE",
    "NotificationEventKind",
    "RoutingDecision",
    "RoutingPolicy",
    "dedup_key_for",
    "email_recipients_for",
    "notifications_enabled_from_env",
]

#: Master switch. Absent or false means nothing leaves the process.
NOTIFICATIONS_ENABLED_ENV: Final[str] = "SCOUT_NOTIFICATIONS_ENABLED"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

SUPPRESSED_DISABLED: Final[str] = (
    "notifications are disabled (SCOUT_NOTIFICATIONS_ENABLED is not true)"
)
SUPPRESSED_DUPLICATE: Final[str] = "an identical notification was already sent for this dedup_key"


class NotificationEventKind(StrEnum):
    """What kind of fact a notification announces.

    Used for ``quiet_on_success`` and for job-scoped dedup keys; the dedup key for
    objects with their own id (a question, a finding) does not need it.
    """

    QUESTION = "question"
    HIGH_SEVERITY_FINDING = "high_severity_finding"
    VERIFICATION_COMPLETE = "verification_complete"
    APPROVAL_REQUIRED = "approval_required"


#: The default table, documented in the module docstring.
DEFAULT_ROUTING: Final[Mapping[Severity, frozenset[NotificationChannel]]] = MappingProxyType(
    {
        Severity.INFO: frozenset(),
        Severity.LOW: frozenset(),
        Severity.MEDIUM: frozenset({NotificationChannel.GITHUB}),
        Severity.HIGH: frozenset({NotificationChannel.GITHUB, NotificationChannel.GOOGLE_CHAT}),
        Severity.CRITICAL: frozenset(
            {
                NotificationChannel.GITHUB,
                NotificationChannel.GOOGLE_CHAT,
                NotificationChannel.EMAIL,
            }
        ),
    }
)

#: Job-scoped dedup suffixes. A completed job and an approval request are different facts
#: about the same job, so they must not collapse into one key.
_JOB_DEDUP_SUFFIX: Final[Mapping[NotificationEventKind, str]] = MappingProxyType(
    {
        NotificationEventKind.VERIFICATION_COMPLETE: "completed",
        NotificationEventKind.APPROVAL_REQUIRED: "approval_required",
    }
)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The routing outcome for one event.

    ``channels`` is empty exactly when ``suppressed_reason`` is set, so a caller can branch
    on either without risking an event that is both routed and suppressed.
    """

    severity: Severity
    event_kind: NotificationEventKind
    channels: frozenset[NotificationChannel] = frozenset()
    suppressed_reason: str | None = None

    @property
    def should_send(self) -> bool:
        """Whether at least one channel should be attempted."""
        return bool(self.channels) and self.suppressed_reason is None


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Configurable mapping from severity to channels, plus the suppression rules.

    ``notifications_enabled`` defaults to ``None``, meaning "read the environment". Tests
    and embedders can pin it instead of mutating process state.
    """

    channels_by_severity: Mapping[Severity, frozenset[NotificationChannel]] = field(
        default_factory=lambda: dict(DEFAULT_ROUTING)
    )
    notifications_enabled: bool | None = None

    def channels_for(self, severity: Severity) -> frozenset[NotificationChannel]:
        """Return the configured channels for ``severity`` (empty means silent)."""
        return self.channels_by_severity.get(severity, frozenset())

    def with_channels(
        self,
        severity: Severity,
        channels: Iterable[NotificationChannel],
    ) -> RoutingPolicy:
        """Return a copy of this policy with ``severity`` remapped."""
        updated = dict(self.channels_by_severity)
        updated[severity] = frozenset(channels)
        return RoutingPolicy(
            channels_by_severity=updated,
            notifications_enabled=self.notifications_enabled,
        )

    def should_notify(
        self,
        *,
        severity: Severity,
        event_kind: NotificationEventKind,
        repository_config: ScoutRepositoryConfig | NotificationsConfig | None = None,
        recently_sent: bool = False,
    ) -> RoutingDecision:
        """Decide which channels to use, or why nothing should be sent.

        Checks run in the order a reader would expect: is the feature on, was this exact
        fact already announced, does the repository's threshold allow it, does the
        repository want quiet successes, and finally is any channel configured at all.
        """
        if not self._enabled():
            return self._suppressed(severity, event_kind, SUPPRESSED_DISABLED)

        if recently_sent:
            return self._suppressed(severity, event_kind, SUPPRESSED_DUPLICATE)

        repository = _notifications_config(repository_config)
        if repository is not None:
            minimum = _parse_severity(repository.min_severity)
            if severity < minimum:
                reason = (
                    f"severity {severity.value} is below the repository min_severity "
                    f"{minimum.value}"
                )
                return self._suppressed(severity, event_kind, reason)
            if (
                repository.quiet_on_success
                and event_kind is NotificationEventKind.VERIFICATION_COMPLETE
                and severity < Severity.HIGH
            ):
                return self._suppressed(
                    severity,
                    event_kind,
                    "repository quiet_on_success suppresses successful completions",
                )

        channels = self.channels_for(severity)
        if not channels:
            reason = (
                f"severity {severity.value} is deliberately silent by default "
                "(no channel configured)"
            )
            return self._suppressed(severity, event_kind, reason)

        return RoutingDecision(severity=severity, event_kind=event_kind, channels=channels)

    def _enabled(self) -> bool:
        if self.notifications_enabled is not None:
            return self.notifications_enabled
        return notifications_enabled_from_env()

    @staticmethod
    def _suppressed(
        severity: Severity,
        event_kind: NotificationEventKind,
        reason: str,
    ) -> RoutingDecision:
        return RoutingDecision(
            severity=severity,
            event_kind=event_kind,
            channels=frozenset(),
            suppressed_reason=reason,
        )


def notifications_enabled_from_env() -> bool:
    """Whether ``SCOUT_NOTIFICATIONS_ENABLED`` is set to a truthy value.

    Unset means disabled: the default configuration must not attempt network calls.
    """
    return os.environ.get(NOTIFICATIONS_ENABLED_ENV, "").strip().lower() in _TRUTHY


def dedup_key_for(
    event: Question | Finding | VerificationJob | VerificationReport | str,
    *,
    event_kind: NotificationEventKind | None = None,
) -> str:
    """Return a stable key that collapses repeats of the same fact.

    Examples: ``question:{id}:{severity}``, ``finding:{id}:{severity}``,
    ``job:{id}:completed``, ``job:{id}:approval_required``. Stability matters more than
    readability — a key that changes between runs re-notifies, which is the failure this
    exists to prevent.
    """
    if isinstance(event, str):
        return event
    if isinstance(event, Question):
        return f"question:{event.id}:{event.severity.value}"
    if isinstance(event, Finding):
        return f"finding:{event.id}:{event.severity.value}"
    if isinstance(event, VerificationReport):
        return f"report:{event.job_id}:{event.recommendation.value}:{event.commit_sha[:7]}"

    suffix = (
        _JOB_DEDUP_SUFFIX.get(event_kind, event.status.value.lower())
        if event_kind
        else (event.status.value.lower())
    )
    return f"job:{event.id}:{suffix}"


def email_recipients_for(
    repository_config: ScoutRepositoryConfig | NotificationsConfig | None = None,
) -> tuple[str, ...]:
    """Return the repository's extra email recipients, if any."""
    repository = _notifications_config(repository_config)
    if repository is None:
        return ()
    return tuple(address for address in repository.email_recipients if address.strip())


def _notifications_config(
    repository_config: ScoutRepositoryConfig | NotificationsConfig | None,
) -> NotificationsConfig | None:
    """Normalise a repository config or a bare notifications config to the latter."""
    if repository_config is None:
        return None
    if isinstance(repository_config, NotificationsConfig):
        return repository_config
    return repository_config.notifications


def _parse_severity(value: str) -> Severity:
    """Parse a configured severity, defaulting to MEDIUM on an unrecognised value.

    The domain model validates the pattern, so this only guards against a hand-edited
    object; MEDIUM is the safe default because it is the threshold at which Scout starts
    interrupting people.
    """
    try:
        return Severity(value)
    except ValueError:
        return Severity.MEDIUM
