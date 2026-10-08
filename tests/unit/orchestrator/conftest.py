"""Fixtures shared by the orchestrator unit tests.

The tests in this directory must run with **no database and no Redis**, because the fast suite is
the one that runs on every change. :class:`fakes.FakeUnitOfWork` provides the persistence seam;
this module wires it into the orchestrator under test.

``fakes`` is imported as a top-level module because this directory has no ``__init__.py``: pytest's
default import mode puts the test file's own directory on ``sys.path``, which is the convention the
rest of this suite follows.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# pytest imports a directory's ``conftest.py`` before it puts that directory on ``sys.path``, so the
# sibling test-double module has to be made importable explicitly. Documented here rather than in
# every test module because this is the only place it is needed.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeUnitOfWork, RecordingSleep, make_job_row

from scout_domain.enums import JobStatus
from scout_github import FakeGitHubClient
from scout_orchestrator.orchestrator import Orchestrator
from scout_orchestrator.settings import Settings
from scout_persistence.mapping import to_verification_job
from scout_persistence.queue import InMemoryJobQueue
from scout_tools.artifacts import InMemoryArtifactStore

if TYPE_CHECKING:
    from scout_domain.models.job import VerificationJob


@pytest.fixture
def uow() -> FakeUnitOfWork:
    """A fake unit of work backed by dictionaries."""
    return FakeUnitOfWork()


@pytest.fixture
def sleep() -> RecordingSleep:
    """An instant, recording replacement for ``asyncio.sleep``."""
    return RecordingSleep()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Orchestrator settings rooted in a temporary directory.

    Built directly rather than from the environment so a developer's ``.env`` cannot change what a
    test asserts.
    """
    return Settings(
        env="test",
        database_url="postgresql+asyncpg://scout:scout@localhost:5433/scout",
        redis_url="redis://localhost:6380/0",
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspaces",
        notifications_enabled=False,
        execution_mode="in_process",
        retry_base_delay_seconds=1.0,
        retry_max_delay_seconds=8.0,
        retry_jitter_ratio=0.0,
        sandbox_backend="local",
    )


@pytest.fixture
def queue() -> InMemoryJobQueue:
    """An in-memory job transport."""
    return InMemoryJobQueue()


@pytest.fixture
def orchestrator(
    settings: Settings,
    uow: FakeUnitOfWork,
    queue: InMemoryJobQueue,
    sleep: RecordingSleep,
    monkeypatch: pytest.MonkeyPatch,
) -> Orchestrator:
    """An orchestrator wired to the fake unit of work and an instant sleep.

    ``UnitOfWork`` is patched in the module that constructs it, so every persistence call in the
    orchestrator under test lands in the fake.
    """
    import scout_orchestrator.orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "UnitOfWork", lambda _factory: uow)
    return Orchestrator(
        settings=settings,
        session_factory=lambda: None,
        artifacts=InMemoryArtifactStore(),
        github=FakeGitHubClient(),
        queue=queue,
        sleep=sleep,
        consumer="orchestrator:test:1",
    )


@pytest.fixture
def job(uow: FakeUnitOfWork) -> VerificationJob:
    """A job in ``RECEIVED``, ready to enter the pipeline, already persisted in the fake."""
    row = uow.add_job(make_job_row(status=JobStatus.RECEIVED))
    return to_verification_job(row)
