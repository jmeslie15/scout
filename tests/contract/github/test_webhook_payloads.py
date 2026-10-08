"""Contract: the parser accepts every captured-shape ``pull_request`` delivery.

The fixtures in ``tests/fixtures/github`` are written to GitHub's documented payload
schema, including the real quirks Scout has to tolerate: an absent ``draft``, a ``null``
``body``, an absent ``installation`` block, and root-level ``before``/``after`` only on
``synchronize``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scout_github.errors import WebhookPayloadError, WebhookSignatureError
from scout_github.webhooks import (
    SUPPORTED_PULL_REQUEST_ACTIONS,
    parse_pull_request_event,
    should_process,
    verify_webhook_signature,
)

BASE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"
OTHER_SHA = "1122334455667788990011223344556677889900"
BEFORE_SHA = "9c8b7a6f5e4d3c2b1a0918273645f7e8d9c0b1a2"

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

#: What each fixture must project to. A fixture that changes without this table changing
#: is a contract change and should fail here.
EXPECTATIONS: dict[str, dict[str, Any]] = {
    "pr_opened.json": {
        "processed": True,
        "action": "opened",
        "installation_id": 9876543,
        "repository_full_name": "edrevel/scout-demo",
        "repository_github_id": 741852963,
        "pull_request_number": 42,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "head_ref": "feature/acceptance-criteria",
        "base_ref": "main",
        "author": "dev-author",
        "draft": False,
        "merged": False,
        "body_is_none": False,
        "before_sha": None,
        "after_sha": None,
    },
    "pr_reopened_body_null.json": {
        "processed": True,
        "action": "reopened",
        "installation_id": 9876543,
        "repository_full_name": "edrevel/scout-demo",
        "pull_request_number": 88,
        "head_sha": OTHER_SHA,
        "base_sha": BASE_SHA,
        "head_ref": "harden/webhook-signature",
        "author": "dev-author",
        "draft": False,
        "merged": False,
        "body_is_none": True,
        "before_sha": None,
        "after_sha": None,
    },
    "pr_synchronize.json": {
        "processed": True,
        "action": "synchronize",
        "installation_id": 9876543,
        "repository_full_name": "edrevel/scout-demo",
        "pull_request_number": 77,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "head_ref": "fix/flaky-retry-test",
        "author": "dev-author",
        "draft": False,
        "merged": False,
        "body_is_none": False,
        "before_sha": BEFORE_SHA,
        "after_sha": HEAD_SHA,
    },
    "pr_synchronize_no_installation.json": {
        "processed": True,
        "action": "synchronize",
        "installation_id": None,
        "repository_full_name": "edrevel/scout-demo",
        "pull_request_number": 91,
        "head_sha": OTHER_SHA,
        "base_sha": BASE_SHA,
        "head_ref": "fix/no-installation",
        "author": "dev-author",
        "draft": True,
        "merged": False,
        "body_is_none": True,
        "before_sha": BEFORE_SHA,
        "after_sha": OTHER_SHA,
    },
    "pr_closed.json": {
        "processed": False,
        "action": "closed",
        "pull_request_number": 42,
    },
}


@pytest.fixture(scope="session")
def fixture_names(fixtures_dir: Path) -> list[str]:
    """Fixture filenames, sorted for deterministic parametrisation."""
    return sorted(path.name for path in fixtures_dir.glob("*.json"))


def test_every_fixture_has_documented_expectations(fixture_names: list[str]) -> None:
    assert fixture_names == sorted(EXPECTATIONS)


def test_fixtures_are_valid_json_with_the_github_envelope(
    fixture_names: list[str], load_fixture: Callable[[str], dict[str, Any]]
) -> None:
    for name in fixture_names:
        payload = load_fixture(name)

        assert set(payload) >= {"action", "number", "pull_request", "repository"}
        assert isinstance(payload["pull_request"], dict)
        assert isinstance(payload["repository"], dict)
        assert payload["repository"]["full_name"] == "edrevel/scout-demo"


def test_fixture_shas_are_git_shaped(
    fixture_names: list[str], load_fixture: Callable[[str], dict[str, Any]]
) -> None:
    for name in fixture_names:
        pull_request = load_fixture(name)["pull_request"]

        assert SHA_PATTERN.match(pull_request["head"]["sha"])
        assert SHA_PATTERN.match(pull_request["base"]["sha"])


@pytest.mark.parametrize("name", sorted(EXPECTATIONS))
def test_parser_projects_each_fixture(
    name: str, load_fixture: Callable[[str], dict[str, Any]]
) -> None:
    expectation = EXPECTATIONS[name]
    payload = load_fixture(name)
    action = payload["action"]

    assert should_process("pull_request", action) is expectation["processed"]

    if not expectation["processed"]:
        with pytest.raises(WebhookPayloadError, match="unsupported pull_request action"):
            parse_pull_request_event(payload, event_type="pull_request", action=action)
        return

    event = parse_pull_request_event(payload, event_type="pull_request", action=action)

    assert event.action == expectation["action"]
    assert event.installation_id == expectation["installation_id"]
    assert event.repository.full_name == expectation["repository_full_name"]
    assert event.pull_request_number == expectation["pull_request_number"]
    assert event.head_sha == expectation["head_sha"]
    assert event.base_sha == expectation["base_sha"]
    assert event.head_ref == expectation["head_ref"]
    assert event.author == expectation["author"]
    assert event.draft is expectation["draft"]
    assert event.merged is expectation["merged"]
    assert (event.body is None) is expectation["body_is_none"]
    assert event.before_sha == expectation["before_sha"]
    assert event.after_sha == expectation["after_sha"]
    assert event.delivery_id is None
    assert event.html_url.endswith(f"/pull/{event.pull_request_number}")

    if "repository_github_id" in expectation:
        assert event.repository.github_id == expectation["repository_github_id"]


@pytest.mark.parametrize("name", sorted(EXPECTATIONS))
def test_fixture_action_is_either_supported_or_deliberately_ignored(
    name: str, load_fixture: Callable[[str], dict[str, Any]]
) -> None:
    action = load_fixture(name)["action"]

    assert action in SUPPORTED_PULL_REQUEST_ACTIONS or name == "pr_closed.json"


def test_closed_delivery_is_ignored_cheaply_and_not_parsed(
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    payload = load_fixture("pr_closed.json")

    assert should_process("pull_request", "closed") is False
    with pytest.raises(WebhookPayloadError):
        parse_pull_request_event(payload, event_type="pull_request", action="closed")


# -- Signature contract over the real bytes ------------------------------------------


def test_every_fixture_signature_verifies_over_the_raw_bytes(
    webhook_fixtures: list[tuple[str, bytes]],
) -> None:
    secret = "contract-test-webhook-secret"

    for name, raw_bytes in webhook_fixtures:
        digest = hmac.new(secret.encode(), raw_bytes, hashlib.sha256).hexdigest()

        verify_webhook_signature(
            payload=raw_bytes,
            signature_header=f"sha256={digest}",
            secret=secret,
        )
        assert json.loads(raw_bytes), f"{name} must still be valid JSON"


def test_reserialised_fixture_bytes_do_not_verify(
    webhook_fixtures: list[tuple[str, bytes]],
) -> None:
    secret = "contract-test-webhook-secret"
    name, raw_bytes = webhook_fixtures[0]
    digest = hmac.new(secret.encode(), raw_bytes, hashlib.sha256).hexdigest()
    reserialised = json.dumps(json.loads(raw_bytes)).encode()

    with pytest.raises(WebhookSignatureError, match="does not match"):
        verify_webhook_signature(
            payload=reserialised,
            signature_header=f"sha256={digest}",
            secret=secret,
        )
    assert name.endswith(".json")
