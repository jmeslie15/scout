"""Shared configuration for the GitHub contract tests.

The fixtures in ``tests/fixtures/github`` are the captured-shape payloads the parser is
contractually required to accept. They are read as raw bytes so a test can also prove the
signature is computed over exactly the bytes GitHub sent.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "github"


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract tests must be reproducible offline; nothing here may open a socket."""

    def _denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("GitHub contract tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", _denied)
    monkeypatch.setattr(socket.socket, "connect_ex", _denied)
    monkeypatch.setattr(socket, "create_connection", _denied)
    monkeypatch.setattr(socket, "getaddrinfo", _denied)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Directory holding the captured-shape webhook payloads."""
    assert FIXTURES_DIR.is_dir(), f"missing fixture directory: {FIXTURES_DIR}"
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def webhook_fixtures(fixtures_dir: Path) -> list[tuple[str, bytes]]:
    """Every fixture as ``(filename, raw bytes)``, in a stable order."""
    loaded = [(path.name, path.read_bytes()) for path in sorted(fixtures_dir.glob("*.json"))]
    assert loaded, f"no webhook fixtures found in {fixtures_dir}"
    return loaded


@pytest.fixture(scope="session")
def load_fixture(fixtures_dir: Path) -> Callable[[str], dict[str, Any]]:
    """Return a loader that parses one fixture into a JSON object."""

    def _load(name: str) -> dict[str, Any]:
        payload = json.loads((fixtures_dir / name).read_bytes())
        assert isinstance(payload, dict), f"{name} must contain a JSON object"
        return payload

    return _load
