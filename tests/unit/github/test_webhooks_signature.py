"""Webhook signature verification (spec section 10).

The negative cases matter more than the positive one: a verifier that accepts a forged,
truncated, or re-serialised body is worse than no verifier, because it looks safe.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from scout_github.errors import WebhookSignatureError
from scout_github.webhooks import SIGNATURE_PREFIX, verify_webhook_signature

SECRET = "s3cret-webhook-value"
BODY = b'{"action":"opened","number":42}'


def _sign(payload: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def test_correct_signature_is_accepted() -> None:
    assert (
        verify_webhook_signature(
            payload=BODY,
            signature_header=_sign(BODY),
            secret=SECRET,
        )
        is None
    )


def test_uppercase_digest_is_accepted() -> None:
    header = _sign(BODY).upper().replace("SHA256=", SIGNATURE_PREFIX)

    verify_webhook_signature(payload=BODY, signature_header=header, secret=SECRET)


def test_surrounding_whitespace_in_the_header_is_tolerated() -> None:
    verify_webhook_signature(
        payload=BODY,
        signature_header=f"  {_sign(BODY)}  ",
        secret=SECRET,
    )


def test_wrong_signature_is_rejected() -> None:
    with pytest.raises(WebhookSignatureError, match="does not match the request body"):
        verify_webhook_signature(
            payload=BODY,
            signature_header=_sign(b"different body"),
            secret=SECRET,
        )


def test_signature_from_a_different_secret_is_rejected() -> None:
    with pytest.raises(WebhookSignatureError, match="does not match"):
        verify_webhook_signature(
            payload=BODY,
            signature_header=_sign(BODY, "another-secret"),
            secret=SECRET,
        )


def test_truncated_signature_is_rejected() -> None:
    with pytest.raises(WebhookSignatureError, match="does not match"):
        verify_webhook_signature(
            payload=BODY,
            signature_header=_sign(BODY)[:20],
            secret=SECRET,
        )


def test_signature_with_only_the_prefix_is_rejected() -> None:
    with pytest.raises(WebhookSignatureError, match="does not match"):
        verify_webhook_signature(
            payload=BODY,
            signature_header=SIGNATURE_PREFIX,
            secret=SECRET,
        )


@pytest.mark.parametrize("header", [None, "", "   "])
def test_missing_signature_header_is_rejected(header: str | None) -> None:
    with pytest.raises(WebhookSignatureError, match="missing X-Hub-Signature-256"):
        verify_webhook_signature(payload=BODY, signature_header=header, secret=SECRET)


@pytest.mark.parametrize("header", ["sha1=abc123", "deadbeef", "SHA-256=abc", "token abc"])
def test_malformed_signature_header_is_rejected(header: str) -> None:
    with pytest.raises(WebhookSignatureError, match="malformed signature header"):
        verify_webhook_signature(payload=BODY, signature_header=header, secret=SECRET)


def test_non_hexadecimal_ascii_digest_is_a_mismatch() -> None:
    with pytest.raises(WebhookSignatureError, match="does not match"):
        verify_webhook_signature(
            payload=BODY,
            signature_header=f"{SIGNATURE_PREFIX}{'z' * 64}",
            secret=SECRET,
        )


def test_non_ascii_digest_is_rejected_as_malformed() -> None:
    """``compare_digest`` refuses non-ASCII, which can only be a malformed header."""
    with pytest.raises(WebhookSignatureError, match="not hexadecimal"):
        verify_webhook_signature(
            payload=BODY,
            signature_header=f"{SIGNATURE_PREFIX}{'é' * 64}",
            secret=SECRET,
        )


def test_reserialised_body_is_rejected() -> None:
    """GitHub signs the bytes it sent; re-encoding the JSON changes the digest."""
    signed_body = b'{"action": "opened", "number": 42}'
    reserialised = json.dumps(json.loads(signed_body), separators=(",", ":")).encode()

    assert signed_body != reserialised

    with pytest.raises(WebhookSignatureError, match="does not match"):
        verify_webhook_signature(
            payload=reserialised,
            signature_header=_sign(signed_body),
            secret=SECRET,
        )


@pytest.mark.parametrize("secret", ["", "   "])
def test_missing_secret_is_a_hard_error(secret: str) -> None:
    with pytest.raises(WebhookSignatureError, match="no GitHub webhook secret"):
        verify_webhook_signature(
            payload=BODY,
            signature_header=_sign(BODY),
            secret=secret,
        )


def test_empty_body_can_be_signed_and_verified() -> None:
    verify_webhook_signature(
        payload=b"",
        signature_header=_sign(b""),
        secret=SECRET,
    )
