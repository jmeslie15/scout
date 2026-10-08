"""NotificationService: routing, dedup, five-element questions, and Rule 12 failure handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scout_domain.config import ScoutRepositoryConfig
from scout_domain.enums import NotificationChannel, Recommendation, Severity
from scout_domain.models.notification import NotificationRecord, NotificationStatus
from scout_notifications.base import DeliveryResult, NotificationMessage
from scout_notifications.routing import (
    SUPPRESSED_DISABLED,
    NotificationEventKind,
    RoutingPolicy,
)
from scout_notifications.service import (
    InMemoryNotificationHistory,
    NotificationService,
    default_job_url,
    severity_for_report,
)
from scout_notifications.templating import TemplateRenderer, build_environment

CENTRAL_JOB_URL = "https://scout.test/jobs/job_1"


def _service(
    *,
    adapters,
    renderer: TemplateRenderer | None = None,
    history: InMemoryNotificationHistory | None = None,
    routing: RoutingPolicy | None = None,
    resolver=None,
    clock=None,
) -> NotificationService:
    return NotificationService(
        adapters=adapters,
        renderer=renderer,
        history=history,
        routing=routing if routing is not None else RoutingPolicy(notifications_enabled=True),
        repository_config_resolver=resolver,
        job_url_builder=lambda job: f"https://scout.test/jobs/{job.id}",
        clock=clock if clock is not None else (lambda: datetime.now(UTC)),
    )


# ---------------------------------------------------------------------------------
# The question contract (spec sections 28 and 72)
# ---------------------------------------------------------------------------------


async def test_question_notification_contains_the_five_required_elements(
    fake_adapter,
    make_question,
    make_job,
) -> None:
    adapter = fake_adapter(NotificationChannel.GITHUB)
    service = _service(adapters=[adapter])
    question = make_question()
    job = make_job()

    records = await service.notify_question(question, job)

    assert len(records) == 1
    assert records[0].status is NotificationStatus.SENT
    assert records[0].channel is NotificationChannel.GITHUB
    assert records[0].question_id == question.id
    assert records[0].job_id == job.id
    assert records[0].dedup_key == "question:qst_1:MEDIUM"

    body = adapter.sent[0].body
    # 1. the question itself
    assert question.title in body
    assert question.description in body
    # 2. why Scout is asking
    assert question.context in body
    assert question.investigation_summary in body
    # 3. the relevant evidence
    assert "app/api/export.py" in body
    assert "no authorization decorator" in body
    # 4. a link to the job
    assert CENTRAL_JOB_URL in body
    assert adapter.sent[0].job_url == CENTRAL_JOB_URL
    # 5. the suggested options
    assert "Managers may see metadata" in body
    assert "Only owners may" in body


async def test_question_message_carries_github_metadata(
    fake_adapter,
    make_question,
    make_job,
) -> None:
    adapter = fake_adapter(NotificationChannel.GITHUB)
    service = _service(adapters=[adapter])

    await service.notify_question(make_question(), make_job())

    metadata = adapter.sent[0].metadata
    assert metadata["repository_full_name"] == "acme/api"
    assert metadata["pull_request_number"] == "42"
    assert metadata["job_id"] == "job_1"


async def test_records_validate_against_the_domain_model(
    fake_adapter,
    make_question,
    make_job,
) -> None:
    adapter = fake_adapter(NotificationChannel.GITHUB)
    service = _service(adapters=[adapter])

    records = await service.notify_question(make_question(), make_job())

    for record in records:
        round_tripped = NotificationRecord.model_validate(record.model_dump())
        assert round_tripped == record


# ---------------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------------


async def test_dedup_suppresses_a_repeat(fake_adapter, make_question, make_job) -> None:
    adapter = fake_adapter(NotificationChannel.GITHUB)
    service = _service(adapters=[adapter])
    question = make_question()
    job = make_job()

    first = await service.notify_question(question, job)
    second = await service.notify_question(question, job)

    assert [record.status for record in first] == [NotificationStatus.SENT]
    assert [record.status for record in second] == [NotificationStatus.SUPPRESSED]
    assert second[0].error is not None
    assert "already sent" in second[0].error
    assert len(adapter.sent) == 1


async def test_dedup_allows_a_content_change_through(
    fake_adapter,
    make_question,
    make_job,
) -> None:
    github = fake_adapter(NotificationChannel.GITHUB)
    chat = fake_adapter(NotificationChannel.GOOGLE_CHAT)
    service = _service(adapters=[github, chat])
    job = make_job()

    await service.notify_question(make_question(severity=Severity.MEDIUM), job)
    changed = await service.notify_question(make_question(severity=Severity.HIGH), job)

    # The severity change alters the dedup key, so the higher-severity question is announced
    # on both channels rather than being folded into the earlier MEDIUM notification.
    assert {record.channel for record in changed} == {
        NotificationChannel.GITHUB,
        NotificationChannel.GOOGLE_CHAT,
    }
    assert all(record.status is NotificationStatus.SENT for record in changed)
    assert len(github.sent) == 2
    assert len(chat.sent) == 1


async def test_dedup_window_expires(fake_adapter, make_question, make_job) -> None:
    clock = [datetime(2025, 6, 1, 12, 0, tzinfo=UTC)]
    history = InMemoryNotificationHistory(clock=lambda: clock[0])
    adapter = fake_adapter(NotificationChannel.GITHUB)
    service = _service(adapters=[adapter], history=history, clock=lambda: clock[0])
    question = make_question()
    job = make_job()

    await service.notify_question(question, job)
    clock[0] += timedelta(hours=7)
    second = await service.notify_question(question, job)

    assert second[0].status is NotificationStatus.SENT
    assert len(adapter.sent) == 2


def test_history_reports_recent_sends_within_the_window() -> None:
    now = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    history = InMemoryNotificationHistory(clock=lambda: now)
    record = NotificationRecord(
        id="ntf_1",
        job_id="job_1",
        channel=NotificationChannel.GITHUB,
        severity=Severity.HIGH,
        subject="Subject",
        body="Body",
        dedup_key="question:qst_1:HIGH",
        status=NotificationStatus.SENT,
        attempts=1,
        sent_at=now - timedelta(minutes=30),
    )
    history.record(record)

    assert history.was_recently_sent("question:qst_1:HIGH", NotificationChannel.GITHUB, 3600)
    # The window is inclusive at its boundary: exactly 30 minutes old is still "recent"
    # for a 30-minute window, and stale for anything shorter.
    assert history.was_recently_sent("question:qst_1:HIGH", NotificationChannel.GITHUB, 1800)
    assert (
        history.was_recently_sent("question:qst_1:HIGH", NotificationChannel.GITHUB, 900) is False
    )
    assert (
        history.was_recently_sent("question:qst_1:HIGH", NotificationChannel.GOOGLE_CHAT, 3600)
        is False
    )


# ---------------------------------------------------------------------------------
# Rule 12: one channel failing must not stop the others
# ---------------------------------------------------------------------------------


async def test_a_failing_channel_returns_a_failed_record_while_the_other_delivers(
    fake_adapter,
    make_question,
    make_job,
) -> None:
    broken = fake_adapter(NotificationChannel.GITHUB, raises=RuntimeError("webhook exploded"))
    working = fake_adapter(NotificationChannel.GOOGLE_CHAT)
    service = _service(adapters=[broken, working])

    records = await service.notify_question(make_question(severity=Severity.HIGH), make_job())

    by_channel = {record.channel: record for record in records}
    assert by_channel[NotificationChannel.GITHUB].status is NotificationStatus.FAILED
    assert by_channel[NotificationChannel.GITHUB].error is not None
    assert "RuntimeError" in by_channel[NotificationChannel.GITHUB].error
    assert by_channel[NotificationChannel.GOOGLE_CHAT].status is NotificationStatus.SENT
    assert len(working.sent) == 1


async def test_channel_reported_failure_is_recorded(fake_adapter, make_question, make_job) -> None:
    refused = fake_adapter(
        NotificationChannel.GITHUB,
        result=DeliveryResult.failure("github returned HTTP 404", retryable=False),
    )
    service = _service(adapters=[refused])

    records = await service.notify_question(make_question(), make_job())

    assert records[0].status is NotificationStatus.FAILED
    assert records[0].error == "github returned HTTP 404"


async def test_missing_adapter_is_a_failed_record(fake_adapter, make_question, make_job) -> None:
    github = fake_adapter(NotificationChannel.GITHUB)
    service = _service(adapters=[github])

    records = await service.notify_question(make_question(severity=Severity.HIGH), make_job())

    by_channel = {record.channel: record for record in records}
    assert by_channel[NotificationChannel.GITHUB].status is NotificationStatus.SENT
    assert by_channel[NotificationChannel.GOOGLE_CHAT].status is NotificationStatus.FAILED
    assert by_channel[NotificationChannel.GOOGLE_CHAT].error is not None
    assert "no adapter" in by_channel[NotificationChannel.GOOGLE_CHAT].error


async def test_rendering_failure_is_recorded_not_raised(
    tmp_path: Path,
    fake_adapter,
    make_question,
    make_job,
) -> None:
    (tmp_path / "question.md.j2").write_text("{{ missing_variable }}", encoding="utf-8")
    renderer = TemplateRenderer(environment=build_environment(tmp_path))
    adapter = fake_adapter(NotificationChannel.GITHUB)
    service = _service(adapters=[adapter], renderer=renderer)

    records = await service.notify_question(make_question(), make_job())

    assert records[0].status is NotificationStatus.FAILED
    assert records[0].error is not None
    assert "question.md.j2" in records[0].error
    assert adapter.sent == []


# ---------------------------------------------------------------------------------
# Suppression policy
# ---------------------------------------------------------------------------------


async def test_notifications_disabled_suppresses_everything(
    monkeypatch: pytest.MonkeyPatch,
    fake_adapter,
    make_question,
    make_job,
) -> None:
    monkeypatch.setenv("SCOUT_NOTIFICATIONS_ENABLED", "false")
    adapter = fake_adapter(NotificationChannel.GITHUB)
    service = NotificationService(adapters=[adapter], routing=RoutingPolicy())

    records = await service.notify_question(make_question(severity=Severity.CRITICAL), make_job())

    assert records
    assert all(record.status is NotificationStatus.SUPPRESSED for record in records)
    assert all(record.error == SUPPRESSED_DISABLED for record in records)
    assert adapter.sent == []


async def test_per_repository_min_severity_override(fake_adapter, make_question, make_job) -> None:
    config = ScoutRepositoryConfig.model_validate(
        {"project": {"name": "acme-api"}, "notifications": {"min_severity": "HIGH"}}
    )
    adapter = fake_adapter(NotificationChannel.GITHUB)
    service = _service(adapters=[adapter], resolver=lambda _: config)

    suppressed = await service.notify_question(make_question(severity=Severity.MEDIUM), make_job())
    delivered = await service.notify_question(make_question(severity=Severity.HIGH), make_job())

    assert all(record.status is NotificationStatus.SUPPRESSED for record in suppressed)
    assert delivered[0].status is NotificationStatus.SENT
    assert len(adapter.sent) == 1


async def test_per_repository_quiet_on_success(fake_adapter, make_report, make_job) -> None:
    config = ScoutRepositoryConfig.model_validate(
        {"project": {"name": "acme-api"}, "notifications": {"quiet_on_success": True}}
    )
    github = fake_adapter(NotificationChannel.GITHUB)
    chat = fake_adapter(NotificationChannel.GOOGLE_CHAT)
    service = _service(adapters=[github, chat], resolver=lambda _: config)

    quiet = await service.notify_verification_complete(
        make_report(recommendation=Recommendation.PASS_WITH_WARNINGS), make_job()
    )
    loud = await service.notify_verification_complete(
        make_report(recommendation=Recommendation.REQUEST_CHANGES), make_job()
    )

    assert all(record.status is NotificationStatus.SUPPRESSED for record in quiet)
    assert {record.channel for record in loud} == {
        NotificationChannel.GITHUB,
        NotificationChannel.GOOGLE_CHAT,
    }
    assert all(record.status is NotificationStatus.SENT for record in loud)


# ---------------------------------------------------------------------------------
# Other events
# ---------------------------------------------------------------------------------


async def test_high_severity_finding_notification(fake_adapter, make_finding, make_job) -> None:
    github = fake_adapter(NotificationChannel.GITHUB)
    chat = fake_adapter(NotificationChannel.GOOGLE_CHAT)
    service = _service(adapters=[github, chat])
    finding = make_finding()
    job = make_job()

    records = await service.notify_high_severity_finding(finding, job)

    assert {record.channel for record in records} == {
        NotificationChannel.GITHUB,
        NotificationChannel.GOOGLE_CHAT,
    }
    assert all(record.finding_id == finding.id for record in records)
    assert all(record.dedup_key == "finding:find_1:HIGH" for record in records)
    assert finding.title in github.sent[0].body
    assert "app/api/export.py:48-61" in github.sent[0].body


async def test_approval_required_uses_high_severity_and_a_stable_key(
    fake_adapter,
    make_job,
) -> None:
    github = fake_adapter(NotificationChannel.GITHUB)
    chat = fake_adapter(NotificationChannel.GOOGLE_CHAT)
    service = _service(adapters=[github, chat])
    job = make_job()

    records = await service.notify_approval_required(job)

    assert {record.channel for record in records} == {
        NotificationChannel.GITHUB,
        NotificationChannel.GOOGLE_CHAT,
    }
    assert all(record.dedup_key == "job:job_1:approval_required" for record in records)
    assert all(record.severity is Severity.HIGH for record in records)
    assert "Approval required" in github.sent[0].body


async def test_critical_verification_complete_reaches_email_with_html_and_recipients(
    fake_adapter,
    make_finding,
    make_report,
    make_job,
) -> None:
    config = ScoutRepositoryConfig.model_validate(
        {
            "project": {"name": "acme-api"},
            "notifications": {"email_recipients": ["oncall@acme.test"]},
        }
    )
    github = fake_adapter(NotificationChannel.GITHUB)
    chat = fake_adapter(NotificationChannel.GOOGLE_CHAT)
    email = fake_adapter(NotificationChannel.EMAIL)
    service = _service(adapters=[github, chat, email], resolver=lambda _: config)
    report = make_report(
        recommendation=Recommendation.FAIL,
        finding=make_finding(severity=Severity.CRITICAL),
    )

    records = await service.notify_verification_complete(report, make_job())

    assert {record.channel for record in records} == {
        NotificationChannel.GITHUB,
        NotificationChannel.GOOGLE_CHAT,
        NotificationChannel.EMAIL,
    }
    assert all(record.status is NotificationStatus.SENT for record in records)
    message: NotificationMessage = email.sent[0]
    assert message.metadata["email_recipients"] == "oncall@acme.test"
    assert "<html" in message.metadata["html_body"]
    assert report.executive_summary in message.body


@pytest.mark.parametrize(
    ("recommendation", "expected"),
    [
        (Recommendation.FAIL, Severity.CRITICAL),
        (Recommendation.REQUEST_CHANGES, Severity.HIGH),
        (Recommendation.REQUIRES_HUMAN_REVIEW, Severity.HIGH),
        (Recommendation.PASS_WITH_WARNINGS, Severity.MEDIUM),
        (Recommendation.PASS, Severity.INFO),
    ],
)
def test_severity_for_report(make_report, recommendation, expected) -> None:
    assert severity_for_report(make_report(recommendation=recommendation)) is expected


def test_a_clean_pass_is_silent_by_default(make_report) -> None:
    """INFO routes nowhere by design; the report is the record of a clean pass."""
    decision = RoutingPolicy(notifications_enabled=True).should_notify(
        severity=severity_for_report(make_report(recommendation=Recommendation.PASS)),
        event_kind=NotificationEventKind.VERIFICATION_COMPLETE,
    )

    assert decision.should_send is False
    assert decision.suppressed_reason is not None


# ---------------------------------------------------------------------------------
# Job URLs
# ---------------------------------------------------------------------------------


def test_default_job_url_prefers_the_dashboard(monkeypatch: pytest.MonkeyPatch, make_job) -> None:
    monkeypatch.setenv("SCOUT_DASHBOARD_URL", "https://scout.test/")
    monkeypatch.setenv("SCOUT_PUBLIC_URL", "https://api.scout.test")

    assert default_job_url(make_job()) == "https://scout.test/jobs/job_1"


def test_default_job_url_falls_back_to_the_pull_request(
    monkeypatch: pytest.MonkeyPatch,
    make_job,
) -> None:
    monkeypatch.delenv("SCOUT_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("SCOUT_PUBLIC_URL", raising=False)

    assert default_job_url(make_job()) == "https://github.com/acme/api/pull/42"


def test_default_job_url_always_returns_something(
    monkeypatch: pytest.MonkeyPatch,
    make_job,
) -> None:
    monkeypatch.delenv("SCOUT_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("SCOUT_PUBLIC_URL", raising=False)
    job = make_job(pull_request_url=None)

    assert default_job_url(job) == "scout://jobs/job_1"
