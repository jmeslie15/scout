"""Fixtures for notification contract tests.

Contract tests own their payload fixtures rather than reaching into the unit-test
conftest: the point of this directory is to pin externally visible shapes, so it should be
readable without following imports into another suite.
"""

from __future__ import annotations

from collections.abc import Callable

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

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


class FakeAdapter:
    """A structurally compatible :class:`NotificationChannelAdapter` for tests."""

    def __init__(self, name: NotificationChannel) -> None:
        self.name = name
        self.sent: list[NotificationMessage] = []

    def supports(self, channel: NotificationChannel) -> bool:
        """Whether this adapter serves ``channel``."""
        return channel is self.name

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """Record the message and report success."""
        self.sent.append(message)
        return DeliveryResult.success("contract-message-id")


@pytest.fixture
def fake_adapter() -> Callable[[NotificationChannel], FakeAdapter]:
    """Return a factory for :class:`FakeAdapter`."""

    def _make(name: NotificationChannel = NotificationChannel.GITHUB) -> FakeAdapter:
        return FakeAdapter(name)

    return _make


@pytest.fixture
def make_job() -> Callable[..., VerificationJob]:
    """Return a factory for a valid :class:`VerificationJob`."""

    def _make() -> VerificationJob:
        return VerificationJob(
            id="job_1",
            repository=RepositoryRef(full_name="acme/api"),
            repository_full_name="acme/api",
            pull_request_number=42,
            pull_request_title="Add export metadata endpoint",
            pull_request_author="dana",
            pull_request_url="https://github.com/acme/api/pull/42",
            commits=CommitRange(base_sha=BASE_SHA, head_sha=HEAD_SHA),
            correlation_id="corr_1",
        )

    return _make


@pytest.fixture
def make_question() -> Callable[..., Question]:
    """Return a factory for a valid :class:`Question`."""

    def _make(*, severity: Severity = Severity.MEDIUM) -> Question:
        return Question(
            id="qst_1",
            job_id="job_1",
            title="Should managers see export metadata?",
            description="The export endpoint returns metadata for every user.",
            context="Scout could not determine the intended authorization model.",
            investigation_summary="Checked route decorators, policy files, and tests.",
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
            options=["Managers may see metadata", "Only owners may"],
            default_assumption="Assume metadata is restricted to owners.",
            created_by="code_reviewer",
        )

    return _make


@pytest.fixture
def make_finding() -> Callable[..., Finding]:
    """Return a factory for a valid :class:`Finding`."""

    def _make(*, severity: Severity = Severity.HIGH) -> Finding:
        return Finding(
            id="find_1",
            job_id="job_1",
            severity=severity,
            category=FindingCategory.SECURITY,
            title="Export endpoint bypasses authorization",
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
        recommendation: Recommendation = Recommendation.REQUEST_CHANGES,
        finding: Finding | None = None,
    ) -> VerificationReport:
        return VerificationReport(
            job_id="job_1",
            repository_full_name="acme/api",
            pull_request_number=42,
            commit_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            executive_summary="Verification completed; one warning requires review.",
            code_review=[finding] if finding is not None else [],
            tests=TestOutcomeSummary(total=10, passed=9, failed=0, skipped=1),
            recommendation=recommendation,
            unverified_areas=["Behaviour under concurrent exports was not measured."],
        )

    return _make
