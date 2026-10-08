"""Shared test configuration.

Every test runs with deterministic, offline-safe defaults: no network, no real model
provider, no real GitHub installation, and artifact/workspace roots redirected into a
temporary directory. A test that needs a live dependency must say so with one of the
markers registered in ``pyproject.toml`` (``integration``, ``e2e``, ``sandbox``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_configure(config: pytest.Config) -> None:
    """Force offline, hermetic defaults before any Scout module is imported."""
    os.environ.setdefault("SCOUT_ENV", "test")
    os.environ.setdefault("SCOUT_LOG_LEVEL", "WARNING")
    os.environ.setdefault("SCOUT_LLM_DEFAULT_PROVIDER", "fake")
    os.environ.setdefault("SCOUT_NOTIFICATIONS_ENABLED", "false")
    os.environ.setdefault("SCOUT_SANDBOX_BACKEND", "local")
    os.environ.setdefault("SCOUT_OTEL_ENABLED", "false")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the Scout repository root."""
    return REPO_ROOT


@pytest.fixture
def artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the artifact store into a per-test temporary directory."""
    target = tmp_path / "artifacts"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SCOUT_ARTIFACT_ROOT", str(target))
    return target


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the sandbox workspace root into a per-test temporary directory."""
    target = tmp_path / "workspaces"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SCOUT_WORKSPACE_ROOT", str(target))
    return target
