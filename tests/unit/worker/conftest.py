"""Fixtures for the worker unit tests.

The big in-memory persistence double lives beside the orchestrator tests; this conftest puts that
directory on ``sys.path`` and reuses it, because the worker and the orchestrator talk to the same
repositories and a second copy would drift. Both directories belong to the same component.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator"))

from fakes import FakeUnitOfWork, RecordingSleep, make_job_row

from scout_domain.enums import JobStatus
from scout_github import FakeGitHubClient
from scout_persistence.mapping import to_verification_job
from scout_persistence.queue import InMemoryJobQueue
from scout_tools.artifacts import InMemoryArtifactStore
from scout_worker.settings import Settings
from scout_worker.worker import Worker

if TYPE_CHECKING:
    from scout_domain.models.job import VerificationJob


@pytest.fixture
def uow() -> FakeUnitOfWork:
    """A fake unit of work backed by dictionaries."""
    return FakeUnitOfWork()


@pytest.fixture
def sleep() -> RecordingSleep:
    """An instant replacement for ``asyncio.sleep``."""
    return RecordingSleep()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Worker settings rooted in a temporary directory."""
    return Settings(
        env="test",
        database_url="postgresql+asyncpg://scout:scout@localhost:5433/scout",
        redis_url="redis://localhost:6380/0",
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspaces",
        sandbox_backend="local",
    )


@pytest.fixture
def queue() -> InMemoryJobQueue:
    """An in-memory transport."""
    return InMemoryJobQueue()


@pytest.fixture
def worker(
    settings: Settings,
    uow: FakeUnitOfWork,
    queue: InMemoryJobQueue,
    sleep: RecordingSleep,
    monkeypatch: pytest.MonkeyPatch,
) -> Worker:
    """A worker wired to the fake unit of work and an instant sleep."""
    import scout_worker.worker as worker_module

    monkeypatch.setattr(worker_module, "UnitOfWork", lambda _factory: uow)
    return Worker(
        settings=settings,
        session_factory=lambda: None,
        github=FakeGitHubClient(),
        queue=queue,
        artifacts=InMemoryArtifactStore(),
        sleep=sleep,
        consumer="worker:test:1",
    )


@pytest.fixture
def job(uow: FakeUnitOfWork) -> VerificationJob:
    """A job sitting in ``PREPARING``, which is the stage the orchestrator would dispatch."""
    row = uow.add_job(make_job_row(status=JobStatus.PREPARING))
    return to_verification_job(row)
