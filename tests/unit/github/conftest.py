"""Shared fixtures for the GitHub unit tests.

Every test in this directory runs behind a socket guard. A unit test that reaches the
real network is a bug that shows up as a flake in CI, and the respx-based tests never
open a socket, so the guard costs nothing and removes the possibility entirely.
"""

from __future__ import annotations

import socket

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test tries to open an outbound connection or resolve a name.

    Only *outbound* operations are patched. ``socket.socket`` itself must stay usable
    because :mod:`asyncio` builds its self-pipe with ``socket.socketpair()`` when the
    event loop is created; blocking the constructor would break every async test rather
    than the network access this guard exists to catch.
    """

    def _denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("GitHub unit tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", _denied)
    monkeypatch.setattr(socket.socket, "connect_ex", _denied)
    monkeypatch.setattr(socket, "create_connection", _denied)
    monkeypatch.setattr(socket, "getaddrinfo", _denied)


@pytest.fixture(scope="session")
def rsa_private_key_pem() -> str:
    """A throwaway 2048-bit RSA key, generated once per test session.

    Generated rather than committed on purpose: a private key checked into a repository
    is precisely the pattern Scout forbids, even when the key only ever signs test data.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
