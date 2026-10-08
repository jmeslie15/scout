"""Settings parsing and the production safety gate (spec section 55).

Configuration is the one place an unsafe combination can be introduced without writing any code, so
the refusal lives in code: ``validate_runtime_safety`` must reject the combinations that would let
repository code run unsandboxed, or point the orchestrator at a development database, in production.

The operator floor is parsed here too, because a typo in ``SCOUT_REQUIRED_STAGES`` must fail at
start-up rather than silently protecting nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scout_domain.enums import JobStatus
from scout_domain.errors import ConfigurationError
from scout_orchestrator.settings import Settings, parse_stage_names


def _settings(**overrides: object) -> Settings:
    """Build settings without reading ``.env`` or the ambient environment."""
    fields: dict[str, object] = {
        "env": "development",
        "database_url": "postgresql+asyncpg://scout:scout@localhost:5433/scout",
        "redis_url": "redis://localhost:6380/0",
        "artifact_root": Path("./var/artifacts"),
        "workspace_root": Path("./var/workspaces"),
        "notifications_enabled": False,
    }
    fields.update(overrides)
    return Settings(**fields)


def test_required_stages_are_parsed_from_a_comma_separated_list() -> None:
    """The documented environment format is a comma-separated list of ``JobStatus`` names."""
    parsed = parse_stage_names("SECURITY_SCANNING, testing", variable="SCOUT_REQUIRED_STAGES")

    assert parsed == frozenset({JobStatus.SECURITY_SCANNING, JobStatus.TESTING})


def test_required_stages_reject_unknown_names() -> None:
    """A name that is not a job status protects nothing, so it fails loudly."""
    with pytest.raises(ConfigurationError) as excinfo:
        parse_stage_names("SECURTY_SCANNING", variable="SCOUT_REQUIRED_STAGES")

    assert "SECURTY_SCANNING" in str(excinfo.value)


def test_empty_required_stages_is_the_default() -> None:
    """The shipped default requires nothing beyond the structural stages."""
    assert _settings().required_stage_set == frozenset()


def test_unknown_execution_mode_is_rejected_at_construction() -> None:
    """Pydantic validates the mode, naming the alternatives."""
    with pytest.raises(ValueError, match="execution mode"):
        _settings(execution_mode="telepathy")


def test_unknown_sandbox_backend_is_rejected_at_construction() -> None:
    """The backend name is validated so ``resolve_backend`` is never handed nonsense."""
    with pytest.raises(ValueError, match="sandbox backend"):
        _settings(sandbox_backend="chroot")


def test_development_allows_the_local_backend() -> None:
    """The local backend is the documented development default."""
    settings = _settings(env="development", sandbox_backend="local")

    settings.validate_runtime_safety()


def test_production_refuses_host_execution() -> None:
    """``local`` runs repository code on the host and is refused in production."""
    settings = _settings(
        env="production",
        sandbox_backend="local",
        database_url="postgresql+asyncpg://scout:scout@db.internal:5432/scout",
        redis_url="redis://redis.internal:6379/0",
        notifications_enabled=True,
    )

    with pytest.raises(ConfigurationError) as excinfo:
        settings.validate_runtime_safety()

    assert "local" in str(excinfo.value)


def test_production_refuses_development_database_credentials() -> None:
    """A production process must not be pointed at the development database."""
    settings = _settings(
        env="production",
        sandbox_backend="docker",
        redis_url="redis://redis.internal:6379/0",
        notifications_enabled=True,
    )

    with pytest.raises(ConfigurationError):
        settings.validate_runtime_safety()


def test_production_refuses_silent_escalation() -> None:
    """An escalation nobody is told about is an escalation nobody answers."""
    settings = _settings(
        env="production",
        sandbox_backend="docker",
        database_url="postgresql+asyncpg://scout:pw@db.internal:5432/scout",
        redis_url="redis://redis.internal:6379/0",
        notifications_enabled=False,
    )

    with pytest.raises(ConfigurationError) as excinfo:
        settings.validate_runtime_safety()

    assert "SCOUT_NOTIFICATIONS_ENABLED" in str(excinfo.value)


def test_production_accepts_a_safe_configuration() -> None:
    """The safe combination starts."""
    settings = _settings(
        env="production",
        sandbox_backend="docker",
        database_url="postgresql+asyncpg://scout:pw@db.internal:5432/scout",
        redis_url="redis://redis.internal:6379/0",
        notifications_enabled=True,
    )

    settings.validate_runtime_safety()


def test_production_validates_required_stages_at_startup() -> None:
    """A bad operator floor fails at start-up, not at the first skip decision."""
    settings = _settings(
        env="production",
        sandbox_backend="docker",
        database_url="postgresql+asyncpg://scout:pw@db.internal:5432/scout",
        redis_url="redis://redis.internal:6379/0",
        notifications_enabled=True,
        required_stages="NOT_A_STAGE",
    )

    with pytest.raises(ConfigurationError):
        settings.validate_runtime_safety()


def test_stage_settings_projection_excludes_credentials(tmp_path: Path) -> None:
    """A stage sees sandbox and git configuration, never the datastore or the webhook secret."""
    settings = _settings(
        artifact_root=tmp_path / "artifacts",
        workspace_root=tmp_path / "workspaces",
        git_token="ghs_secret",
        github_webhook_secret="webhook-secret",
    )

    stage_settings = settings.to_stage_settings()

    assert stage_settings.artifact_root == tmp_path / "artifacts"
    assert stage_settings.git_token == "ghs_secret"
    assert not hasattr(stage_settings, "database_url")
    assert not hasattr(stage_settings, "github_webhook_secret")


def test_worker_execution_mode_requires_a_stage_queue() -> None:
    """Worker mode without a dispatch queue would silently run code in the wrong plane."""
    from scout_orchestrator.errors import OrchestratorError
    from scout_orchestrator.orchestrator import Orchestrator

    settings = _settings(execution_mode="worker")

    with pytest.raises(OrchestratorError, match="stage queue"):
        Orchestrator(
            settings=settings,
            session_factory=lambda: None,
            artifacts=None,
            github=None,
            queue=None,
        )
