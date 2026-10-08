"""Severity routing, anti-spam suppression, and dedup keys (spec section 47)."""

from __future__ import annotations

import pytest

from scout_domain.config import NotificationsConfig, ScoutRepositoryConfig
from scout_domain.enums import NotificationChannel, Recommendation, Severity
from scout_notifications.routing import (
    DEFAULT_ROUTING,
    SUPPRESSED_DISABLED,
    SUPPRESSED_DUPLICATE,
    NotificationEventKind,
    RoutingPolicy,
    dedup_key_for,
    email_recipients_for,
    notifications_enabled_from_env,
)

ALL_SEVERITIES = list(Severity)


@pytest.fixture
def enabled_policy() -> RoutingPolicy:
    """A policy with the feature explicitly on, independent of the process environment."""
    return RoutingPolicy(notifications_enabled=True)


def test_default_routing_table_is_quiet_at_the_bottom() -> None:
    """The documented default: nothing below MEDIUM interrupts anyone."""
    assert DEFAULT_ROUTING[Severity.INFO] == frozenset()
    assert DEFAULT_ROUTING[Severity.LOW] == frozenset()
    assert DEFAULT_ROUTING[Severity.MEDIUM] == frozenset({NotificationChannel.GITHUB})
    assert DEFAULT_ROUTING[Severity.HIGH] == frozenset(
        {NotificationChannel.GITHUB, NotificationChannel.GOOGLE_CHAT}
    )
    assert DEFAULT_ROUTING[Severity.CRITICAL] == frozenset(
        {
            NotificationChannel.GITHUB,
            NotificationChannel.GOOGLE_CHAT,
            NotificationChannel.EMAIL,
        }
    )


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        (Severity.INFO, frozenset()),
        (Severity.LOW, frozenset()),
        (Severity.MEDIUM, frozenset({NotificationChannel.GITHUB})),
        (
            Severity.HIGH,
            frozenset({NotificationChannel.GITHUB, NotificationChannel.GOOGLE_CHAT}),
        ),
        (
            Severity.CRITICAL,
            frozenset(
                {
                    NotificationChannel.GITHUB,
                    NotificationChannel.GOOGLE_CHAT,
                    NotificationChannel.EMAIL,
                }
            ),
        ),
    ],
)
def test_should_notify_for_every_severity(
    enabled_policy: RoutingPolicy,
    severity: Severity,
    expected: frozenset[NotificationChannel],
) -> None:
    decision = enabled_policy.should_notify(
        severity=severity,
        event_kind=NotificationEventKind.QUESTION,
    )

    assert decision.channels == expected
    assert decision.should_send is bool(expected)
    if expected:
        assert decision.suppressed_reason is None
    else:
        assert decision.suppressed_reason is not None
        assert "silent by default" in decision.suppressed_reason


def test_notifications_enabled_false_suppresses_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_NOTIFICATIONS_ENABLED", "false")

    for severity in ALL_SEVERITIES:
        decision = RoutingPolicy().should_notify(
            severity=severity,
            event_kind=NotificationEventKind.QUESTION,
        )
        assert decision.channels == frozenset()
        assert decision.suppressed_reason == SUPPRESSED_DISABLED


def test_notifications_enabled_defaults_to_silent_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCOUT_NOTIFICATIONS_ENABLED", raising=False)

    assert notifications_enabled_from_env() is False
    assert (
        RoutingPolicy()
        .should_notify(severity=Severity.CRITICAL, event_kind=NotificationEventKind.QUESTION)
        .suppressed_reason
        == SUPPRESSED_DISABLED
    )


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " true "])
def test_notifications_enabled_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SCOUT_NOTIFICATIONS_ENABLED", value)
    assert notifications_enabled_from_env() is True


def test_explicit_policy_flag_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOUT_NOTIFICATIONS_ENABLED", "false")

    decision = RoutingPolicy(notifications_enabled=True).should_notify(
        severity=Severity.CRITICAL,
        event_kind=NotificationEventKind.QUESTION,
    )

    assert decision.should_send


def test_recently_sent_is_suppressed_as_a_duplicate(enabled_policy: RoutingPolicy) -> None:
    decision = enabled_policy.should_notify(
        severity=Severity.CRITICAL,
        event_kind=NotificationEventKind.QUESTION,
        recently_sent=True,
    )

    assert decision.channels == frozenset()
    assert decision.suppressed_reason == SUPPRESSED_DUPLICATE


def test_repository_min_severity_override(enabled_policy: RoutingPolicy) -> None:
    config = ScoutRepositoryConfig.model_validate(
        {
            "project": {"name": "acme-api"},
            "notifications": {"min_severity": "HIGH"},
        }
    )

    below = enabled_policy.should_notify(
        severity=Severity.MEDIUM,
        event_kind=NotificationEventKind.QUESTION,
        repository_config=config,
    )
    at_threshold = enabled_policy.should_notify(
        severity=Severity.HIGH,
        event_kind=NotificationEventKind.QUESTION,
        repository_config=config,
    )

    assert below.channels == frozenset()
    assert below.suppressed_reason is not None
    assert "min_severity HIGH" in below.suppressed_reason
    assert at_threshold.should_send


def test_repository_quiet_on_success_override(enabled_policy: RoutingPolicy) -> None:
    config = NotificationsConfig(quiet_on_success=True)

    success = enabled_policy.should_notify(
        severity=Severity.MEDIUM,
        event_kind=NotificationEventKind.VERIFICATION_COMPLETE,
        repository_config=config,
    )
    failure = enabled_policy.should_notify(
        severity=Severity.HIGH,
        event_kind=NotificationEventKind.VERIFICATION_COMPLETE,
        repository_config=config,
    )
    question = enabled_policy.should_notify(
        severity=Severity.MEDIUM,
        event_kind=NotificationEventKind.QUESTION,
        repository_config=config,
    )

    assert success.channels == frozenset()
    assert success.suppressed_reason is not None
    assert "quiet_on_success" in success.suppressed_reason
    assert failure.should_send
    assert question.should_send


def test_repository_min_severity_accepts_the_domain_config_shape() -> None:
    config = NotificationsConfig(min_severity="CRITICAL")

    decision = RoutingPolicy(notifications_enabled=True).should_notify(
        severity=Severity.HIGH,
        event_kind=NotificationEventKind.QUESTION,
        repository_config=config,
    )

    assert decision.channels == frozenset()
    assert decision.suppressed_reason is not None
    assert "min_severity CRITICAL" in decision.suppressed_reason


def test_policy_can_be_reconfigured(enabled_policy: RoutingPolicy) -> None:
    custom = enabled_policy.with_channels(Severity.INFO, {NotificationChannel.EMAIL})

    assert custom.channels_for(Severity.INFO) == frozenset({NotificationChannel.EMAIL})
    assert enabled_policy.channels_for(Severity.INFO) == frozenset()
    assert custom.should_notify(
        severity=Severity.INFO,
        event_kind=NotificationEventKind.QUESTION,
    ).should_send


def test_dedup_key_for_each_event_type(make_job, make_question, make_finding, make_report) -> None:
    question = make_question()
    finding = make_finding()
    job = make_job()
    report = make_report()

    assert dedup_key_for(question) == "question:qst_1:MEDIUM"
    assert dedup_key_for(finding) == "finding:find_1:HIGH"
    assert (
        dedup_key_for(job, event_kind=NotificationEventKind.VERIFICATION_COMPLETE)
        == "job:job_1:completed"
    )
    assert (
        dedup_key_for(job, event_kind=NotificationEventKind.APPROVAL_REQUIRED)
        == "job:job_1:approval_required"
    )
    assert dedup_key_for(job) == "job:job_1:received"
    assert (
        dedup_key_for(report) == f"report:job_1:{Recommendation.PASS_WITH_WARNINGS.value}:bbbbbbb"
    )
    assert dedup_key_for("custom:key") == "custom:key"


def test_dedup_key_changes_with_material_content(make_question) -> None:
    medium = make_question(severity=Severity.MEDIUM)
    high = make_question(severity=Severity.HIGH)

    assert dedup_key_for(medium) != dedup_key_for(high)


def test_email_recipients_for_repository_config() -> None:
    config = ScoutRepositoryConfig.model_validate(
        {
            "project": {"name": "acme-api"},
            "notifications": {"email_recipients": ["oncall@acme.test", "lead@acme.test"]},
        }
    )

    assert email_recipients_for(config) == ("oncall@acme.test", "lead@acme.test")
    assert email_recipients_for(NotificationsConfig()) == ()
    assert email_recipients_for(None) == ()


def test_routing_decision_never_both_sends_and_suppresses() -> None:
    decision = RoutingPolicy(notifications_enabled=True).should_notify(
        severity=Severity.CRITICAL,
        event_kind=NotificationEventKind.APPROVAL_REQUIRED,
    )

    assert decision.should_send
    assert decision.suppressed_reason is None
    assert decision.severity is Severity.CRITICAL
    assert decision.event_kind is NotificationEventKind.APPROVAL_REQUIRED
