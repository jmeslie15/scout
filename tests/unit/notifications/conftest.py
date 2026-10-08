"""Shared fixtures for notification tests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import pytest

from scout_domain.enums import (
    EvidenceKind,
    FindingCategory,
    NotificationChannel,
    Recommendation,
    Severity,
)
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.finding import Finding, FindingOrigin
from scout_domain.models.job import CommitRange, VerificationJob
from scout_domain.models.question import Question
from scout_domain.models.report import TestOutcomeSummary, VerificationReport
from scout_domain.models.repository import RepositoryRef
from scout_notifications.base import DeliveryResult, NotificationMessage

NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


@pytest.fixture
def enable_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn notifications on for a test.

    ``tests/conftest.py`` sets ``SCOUT_NOTIFICATIONS_ENABLED=false`` for the whole suite so
    no test can reach the network by accident; a test that exercises routing opts in.
    """
    monkeypatch.setenv("SCOUT_NOTIFICATIONS_ENABLED", "true")


@pytest.fixture
def make_job() -> Callable[..., VerificationJob]:
    """Return a factory for a valid :class:`VerificationJob`."""

    def _make(
        *,
        id: str = "job_1",  # noqa: A002 - matches the domain field name
        repository_full_name: str = "acme/api",
        pull_request_number: int = 42,
        pull_request_title: str = "Add export metadata endpoint",
        pull_request_url: str | None = "https://github.com/acme/api/pull/42",
    ) -> VerificationJob:
        return VerificationJob(
            id=id,
            repository=RepositoryRef(full_name=repository_full_name),
            repository_full_name=repository_full_name,
            pull_request_number=pull_request_number,
            pull_request_title=pull_request_title,
            pull_request_author="dana",
            pull_request_url=pull_request_url,
            commits=CommitRange(base_sha=BASE_SHA, head_sha=HEAD_SHA),
            correlation_id="corr_1",
        )

    return _make


@pytest.fixture
def make_question() -> Callable[..., Question]:
    """Return a factory for a valid :class:`Question` with evidence and options."""

    def _make(
        *,
        id: str = "qst_1",  # noqa: A002 - matches the domain field name
        job_id: str = "job_1",
        severity: Severity = Severity.MEDIUM,
        title: str = "Should managers see export metadata?",
        options: Sequence[str] = ("Managers may see metadata", "Only owners may"),
    ) -> Question:
        return Question(
            id=id,
            job_id=job_id,
            title=title,
            description="The export endpoint returns metadata for every user.",
            context="Scout could not determine the intended authorization model.",
            investigation_summary="Checked route decorators, policy files, and existing tests.",
            evidence=[
                EvidenceReference(
                    kind=EvidenceKind.FILE_RANGE,
                    reference_id="file:app/api/export.py",
                    summary="The export route carries no authorization decorator.",
                    file_path="app/api/export.py",
                    line_start=48,
                    line_end=61,
                )
            ],
            severity=severity,
            options=list(options),
            default_assumption="Assume metadata is restricted to owners.",
            created_by="code_reviewer",
        )

    return _make


@pytest.fixture
def make_finding() -> Callable[..., Finding]:
    """Return a factory for a valid machine-backed :class:`Finding`."""

    def _make(
        *,
        id: str = "find_1",  # noqa: A002 - matches the domain field name
        job_id: str = "job_1",
        severity: Severity = Severity.HIGH,
        title: str = "Export endpoint bypasses authorization",
    ) -> Finding:
        return Finding(
            id=id,
            job_id=job_id,
            severity=severity,
            category=FindingCategory.SECURITY,
            title=title,
            description="The export route returns metadata without an authorization check.",
            repository="acme/api",
            file="app/api/export.py",
            line_start=48,
            line_end=61,
            evidence=[
                EvidenceReference(
                    kind=EvidenceKind.TOOL_RUN,
                    reference_id="trun_1",
                    summary="semgrep rule scout.missing-authorization fired",
                )
            ],
            recommendation="Add an explicit authorization check before serialising metadata.",
            origin=FindingOrigin.AGENT,
            source="security",
        )

    return _make


@pytest.fixture
def make_report() -> Callable[..., VerificationReport]:
    """Return a factory for a valid :class:`VerificationReport`."""

    def _make(
        *,
        recommendation: Recommendation = Recommendation.PASS_WITH_WARNINGS,
        finding: Finding | None = None,
    ) -> VerificationReport:
        findings = [finding] if finding is not None else []
        return VerificationReport(
            job_id="job_1",
            repository_full_name="acme/api",
            pull_request_number=42,
            commit_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            executive_summary="Verification completed; one security warning requires review.",
            code_review=findings,
            tests=TestOutcomeSummary(total=10, passed=9, failed=0, skipped=1),
            recommendation=recommendation,
            unverified_areas=["Behaviour under concurrent exports was not measured."],
        )

    return _make


class FakeAdapter:
    """A structurally compatible :class:`NotificationChannelAdapter` for tests."""

    def __init__(
        self,
        name: NotificationChannel,
        *,
        result: DeliveryResult | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self.sent: list[NotificationMessage] = []
        self._result = result
        self._raises = raises

    def supports(self, channel: NotificationChannel) -> bool:
        """Whether this adapter serves ``channel``."""
        return channel is self.name

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """Record the message and return (or raise) the configured outcome."""
        self.sent.append(message)
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result
        return DeliveryResult.success("provider-message-1")


@pytest.fixture
def fake_adapter() -> Callable[..., FakeAdapter]:
    """Return a factory for :class:`FakeAdapter`."""

    def _make(
        name: NotificationChannel = NotificationChannel.GITHUB,
        *,
        result: DeliveryResult | None = None,
        raises: Exception | None = None,
    ) -> FakeAdapter:
        return FakeAdapter(name, result=result, raises=raises)

    return _make
