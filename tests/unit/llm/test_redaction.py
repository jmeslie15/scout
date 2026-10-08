"""Credential redaction (spec sections 38, 55)."""

from __future__ import annotations

from scout_llm.redaction import REDACTED, redact_headers, redact_payload, redact_text

SECRET = "sk-test-abcdef1234567890"


def test_redact_headers_replaces_credentials_case_insensitively() -> None:
    scrubbed = redact_headers(
        {
            "Authorization": f"Bearer {SECRET}",
            "X-Api-Key": SECRET,
            "Content-Type": "application/json",
        }
    )
    assert scrubbed["Authorization"] == REDACTED
    assert scrubbed["X-Api-Key"] == REDACTED
    assert scrubbed["Content-Type"] == "application/json"
    assert SECRET not in str(scrubbed)


def test_redact_headers_keeps_benign_headers_verbatim() -> None:
    scrubbed = redact_headers({"anthropic-version": "2023-06-01"})
    assert scrubbed == {"anthropic-version": "2023-06-01"}


def test_redact_text_removes_known_secret_values() -> None:
    text = f"provider said the key {SECRET} is invalid"
    assert redact_text(text, secrets=(SECRET,)) == f"provider said the key {REDACTED} is invalid"


def test_redact_text_ignores_short_secrets() -> None:
    """A one-character 'secret' would rewrite the payload and destroy its debugging value."""
    assert redact_text("banana", secrets=("a",)) == "banana"


def test_redact_text_leaves_unrelated_text_alone() -> None:
    assert redact_text("nothing to hide", secrets=(SECRET,)) == "nothing to hide"


def test_redact_payload_walks_nested_structures() -> None:
    payload = {
        "error": {"message": f"bad key {SECRET}", "api_key": SECRET},
        "items": [{"authorization": "Bearer x"}, "plain"],
        "count": 3,
    }
    scrubbed = redact_payload(payload, secrets=(SECRET,))

    assert scrubbed["error"]["message"] == f"bad key {REDACTED}"
    assert scrubbed["error"]["api_key"] == REDACTED
    assert scrubbed["items"][0]["authorization"] == REDACTED
    assert scrubbed["items"][1] == "plain"
    assert scrubbed["count"] == 3
    assert SECRET not in str(scrubbed)


def test_redact_payload_is_bounded_in_depth() -> None:
    """A pathological payload must not hang the gateway while it writes a log line."""
    node: dict[str, object] = {"leaf": "value"}
    for _ in range(50):
        node = {"nested": node}
    assert REDACTED in str(redact_payload(node))


def test_redact_payload_passes_through_non_string_scalars() -> None:
    assert redact_payload({"a": None, "b": True, "c": 1.5}) == {"a": None, "b": True, "c": 1.5}
