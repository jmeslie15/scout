"""Webhook signature rejection, tested at the HTTP boundary.

This file exists because the rejection path had no test and was broken.

When a delivery arrived with an invalid signature, HMAC verification correctly raised — and then
the `_log.warning` that recorded the rejection passed `event=`, which collides with structlog's
positional message parameter. The logging call raised `TypeError`, the catch-all handler turned it
into a **500**, and the caller never saw the **401** the security control was supposed to produce.
The check itself was sound; its *reporting* was not, and nothing noticed because no test drove the
rejection path.

So these tests assert the status code and the error body, not that "something raised". They also
pin the ordering property that makes them possible without a database: a request rejected for a
bad signature, malformed JSON, an irrelevant action, or a missing delivery id never touches the
application's dependencies. Authentication happens before work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scout_api.main import create_app
from scout_api.settings import get_settings

SECRET = "unit-test-webhook-secret"

DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"
SIGNATURE_HEADER = "X-Hub-Signature-256"


def _payload(action: str = "opened") -> dict[str, Any]:
    return {
        "action": action,
        "installation": {"id": 4242},
        "repository": {"id": 99, "full_name": "acme/widgets", "default_branch": "main"},
        "pull_request": {
            "number": 7,
            "title": "Add a helper",
            "body": "Adds a helper.",
            "draft": False,
            "merged": False,
            "html_url": "https://github.com/acme/widgets/pull/7",
            "user": {"login": "dev"},
            "base": {"sha": "1" * 40, "ref": "main"},
            "head": {"sha": "2" * 40, "ref": "feature"},
        },
    }


def _body(action: str = "opened", **overrides: Any) -> bytes:
    payload = _payload(action)
    payload.update(overrides)
    return json.dumps(payload).encode()


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@asynccontextmanager
async def _noop_lifespan(_app: Any) -> AsyncIterator[None]:
    """A lifespan that builds nothing.

    ``TestClient`` must be entered as a context manager for its portal to exist, and entering it
    runs the app's lifespan. The real lifespan opens a database pool and a Redis connection, which
    these tests exist to prove are never touched. Replacing it keeps the ASGI plumbing working
    while removing every dependency.
    """
    yield


def _app_with_stub_state() -> Any:
    """Build the app with a stub :class:`AppState` and no dependency construction.

    The stub's dependency fields are ``None``, so any request that reached one of them would raise
    and fail its test. That turns "this path performs no work" into an assertion rather than an
    assumption.
    """
    from scout_api.deps import AppState

    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    app.state.scout = AppState(
        settings=get_settings(),
        engine=None,
        session_factory=None,
        artifacts=None,
        github=None,
    )
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client with signature verification on and no real dependencies."""
    monkeypatch.setenv("SCOUT_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("SCOUT_WEBHOOK_REQUIRE_SIGNATURE", "true")
    monkeypatch.setenv("SCOUT_ENV", "test")
    get_settings.cache_clear()
    with TestClient(_app_with_stub_state(), raise_server_exceptions=False) as test_client:
        yield test_client
    get_settings.cache_clear()


class TestForgedSignatureIsRejected:
    def test_wrong_signature_returns_401_not_500(self, client: TestClient) -> None:
        """The regression: this returned 500 because logging the rejection crashed."""
        body = _body()
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "pull_request",
                DELIVERY_HEADER: "delivery-forged",
                SIGNATURE_HEADER: "sha256=" + "0" * 64,
            },
        )
        assert response.status_code == 401, (
            f"expected 401, got {response.status_code}: {response.text[:400]}\n"
            "A 500 here means the rejection was reported by crashing rather than by answering."
        )
        assert response.json()["detail"]["code"] == "invalid_signature"

    def test_signature_from_the_wrong_secret_is_rejected(self, client: TestClient) -> None:
        body = _body()
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "pull_request",
                DELIVERY_HEADER: "delivery-wrong-secret",
                SIGNATURE_HEADER: _sign(body, secret="not-the-configured-secret"),
            },
        )
        assert response.status_code == 401

    def test_missing_signature_header_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/webhooks/github",
            content=_body(),
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "pull_request",
                DELIVERY_HEADER: "delivery-no-signature",
            },
        )
        assert response.status_code == 401

    def test_a_body_altered_after_signing_is_rejected(self, client: TestClient) -> None:
        """The signature covers the raw bytes, so re-encoding the parsed body must not verify."""
        signed = _body()
        signature = _sign(signed)
        tampered = json.dumps(_payload(action="reopened")).encode()
        assert tampered != signed

        response = client.post(
            "/api/v1/webhooks/github",
            content=tampered,
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "pull_request",
                DELIVERY_HEADER: "delivery-tampered",
                SIGNATURE_HEADER: signature,
            },
        )
        assert response.status_code == 401

    def test_rejection_is_logged_without_raising(self, client: TestClient) -> None:
        """Directly cover the line that used to crash: the rejection must be recordable.

        A 401 is only trustworthy if producing it does not itself throw, so this asserts both
        that the response is correct and that the process handled it cleanly.
        """
        for attempt in range(3):
            response = client.post(
                "/api/v1/webhooks/github",
                content=_body(),
                headers={
                    "Content-Type": "application/json",
                    EVENT_HEADER: "pull_request",
                    DELIVERY_HEADER: f"delivery-repeat-{attempt}",
                    SIGNATURE_HEADER: "sha256=deadbeef",
                },
            )
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "invalid_signature"


class TestIrrelevantDeliveriesAreIgnoredCheaply:
    def test_unsupported_action_returns_ignored(self, client: TestClient) -> None:
        body = _body(action="closed")
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "pull_request",
                DELIVERY_HEADER: "delivery-closed",
                SIGNATURE_HEADER: _sign(body),
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] == "ignored"

    def test_non_pull_request_event_returns_ignored(self, client: TestClient) -> None:
        body = _body()
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "issues",
                DELIVERY_HEADER: "delivery-issues",
                SIGNATURE_HEADER: _sign(body),
            },
        )
        assert response.status_code == 202
        assert response.json()["status"] == "ignored"


class TestMalformedInputIsRejected:
    def test_missing_delivery_id_returns_400(self, client: TestClient) -> None:
        """Without an idempotency key the delivery cannot be deduplicated, so it is refused."""
        body = _body()
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "pull_request",
                SIGNATURE_HEADER: _sign(body),
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "missing_delivery_id"

    def test_invalid_json_returns_400_not_500(self, client: TestClient) -> None:
        body = b"{ not json"
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "pull_request",
                DELIVERY_HEADER: "delivery-bad-json",
                SIGNATURE_HEADER: _sign(body),
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_json"

    def test_payload_missing_shas_returns_400(self, client: TestClient) -> None:
        payload = _payload()
        del payload["pull_request"]["head"]["sha"]  # type: ignore[index]
        body = json.dumps(payload).encode()
        response = client.post(
            "/api/v1/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                EVENT_HEADER: "pull_request",
                DELIVERY_HEADER: "delivery-no-sha",
                SIGNATURE_HEADER: _sign(body),
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_pull_request_payload"


class TestProductionRefusesToStartUnprotected:
    def test_signature_verification_required_without_a_secret_is_a_500_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verification cannot be silently skipped when the secret is missing.

        The handler must refuse the request rather than treat an unverifiable delivery as valid.
        """
        monkeypatch.setenv("SCOUT_WEBHOOK_REQUIRE_SIGNATURE", "true")
        monkeypatch.delenv("SCOUT_GITHUB_WEBHOOK_SECRET", raising=False)
        monkeypatch.setenv("SCOUT_ENV", "test")
        get_settings.cache_clear()
        try:
            with TestClient(_app_with_stub_state(), raise_server_exceptions=False) as app_client:
                body = _body()
                response = app_client.post(
                    "/api/v1/webhooks/github",
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        EVENT_HEADER: "pull_request",
                        DELIVERY_HEADER: "delivery-no-secret",
                        SIGNATURE_HEADER: _sign(body),
                    },
                )
                # 500 because the server is misconfigured -- the point is that it is NOT a 202,
                # so an unverifiable delivery is never accepted as work.
                assert response.status_code == 500
                assert response.json()["detail"]["code"] == "webhook_secret_missing"
        finally:
            get_settings.cache_clear()
