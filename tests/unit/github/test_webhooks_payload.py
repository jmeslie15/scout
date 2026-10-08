"""Pull-request payload parsing, including GitHub's real payload quirks."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from scout_github.errors import WebhookPayloadError
from scout_github.webhooks import (
    PLANNED_EVENT_TYPES,
    PULL_REQUEST_EVENT_TYPE,
    SUPPORTED_PULL_REQUEST_ACTIONS,
    PullRequestEvent,
    parse_pull_request_event,
    should_process,
)

BASE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"


def _payload(**overrides: Any) -> dict[str, Any]:
    """A minimal but realistic ``pull_request`` delivery."""
    payload: dict[str, Any] = {
        "action": "opened",
        "number": 42,
        "pull_request": {
            "number": 42,
            "title": "Add acceptance-criteria verification",
            "body": "Closes #7",
            "draft": False,
            "merged": False,
            "html_url": "https://github.com/edrevel/scout-demo/pull/42",
            "user": {"login": "dev-author"},
            "base": {"ref": "main", "sha": BASE_SHA},
            "head": {"ref": "feature/verification", "sha": HEAD_SHA},
        },
        "repository": {
            "id": 741852963,
            "full_name": "edrevel/scout-demo",
            "default_branch": "main",
        },
        "installation": {"id": 9876543},
    }
    for key, value in overrides.items():
        # Only the pull request object is merged field-by-field; every other override,
        # including an empty installation or repository block, replaces the default
        # outright so that "field missing" cases are expressible.
        if key == "pull_request" and isinstance(value, dict):
            payload[key] = {**payload[key], **value}
        else:
            payload[key] = value
    return payload


def _parse(payload: dict[str, Any], *, action: str = "opened") -> PullRequestEvent:
    return parse_pull_request_event(
        payload,
        event_type=PULL_REQUEST_EVENT_TYPE,
        action=action,
    )


def test_full_payload_is_projected_onto_the_event() -> None:
    event = _parse(_payload())

    assert event.action == "opened"
    assert event.installation_id == 9876543
    assert event.repository.full_name == "edrevel/scout-demo"
    assert event.repository.github_id == 741852963
    assert event.repository.default_branch == "main"
    assert event.pull_request_number == 42
    assert event.title == "Add acceptance-criteria verification"
    assert event.body == "Closes #7"
    assert event.author == "dev-author"
    assert event.html_url == "https://github.com/edrevel/scout-demo/pull/42"
    assert event.base_sha == BASE_SHA
    assert event.head_sha == HEAD_SHA
    assert event.base_ref == "main"
    assert event.head_ref == "feature/verification"
    assert event.draft is False
    assert event.merged is False


def test_delivery_id_is_left_to_the_caller() -> None:
    event = _parse(_payload())

    assert event.delivery_id is None
    assert event.evolve(delivery_id="d1e2f3").delivery_id == "d1e2f3"


# -- Real payload quirks -------------------------------------------------------------


def test_absent_draft_is_false() -> None:
    payload = _payload()
    del payload["pull_request"]["draft"]

    assert _parse(payload).draft is False


def test_present_draft_is_honoured() -> None:
    payload = _payload(pull_request={"draft": True})

    assert _parse(payload).draft is True


def test_null_body_is_preserved_as_none() -> None:
    payload = _payload(pull_request={"body": None})

    assert _parse(payload).body is None


def test_missing_body_key_is_none() -> None:
    payload = _payload()
    del payload["pull_request"]["body"]

    assert _parse(payload).body is None


def test_absent_installation_is_none() -> None:
    payload = _payload()
    del payload["installation"]

    assert _parse(payload).installation_id is None


@pytest.mark.parametrize("installation", [None, {}, {"id": 0}, {"id": "123"}, {"id": True}])
def test_unusable_installation_blocks_are_none(installation: Any) -> None:
    payload = _payload(installation=installation)

    assert _parse(payload).installation_id is None


def test_missing_user_login_yields_an_empty_author() -> None:
    payload = _payload(pull_request={"user": None})

    assert _parse(payload).author == ""


def test_repository_full_name_is_reconstructed_from_owner_and_name() -> None:
    payload = _payload(
        repository={
            "full_name": "",
            "owner": {"login": "edrevel"},
            "name": "scout-demo",
        }
    )

    assert _parse(payload).repository.full_name == "edrevel/scout-demo"


def test_before_and_after_are_captured_for_a_synchronize_event() -> None:
    payload = _payload(action="synchronize", before="f" * 40, after=HEAD_SHA)

    event = _parse(payload, action="synchronize")

    assert event.before_sha == "f" * 40
    assert event.after_sha == HEAD_SHA


def test_before_and_after_are_absent_for_an_opened_event() -> None:
    event = _parse(_payload())

    assert event.before_sha is None
    assert event.after_sha is None


def test_merged_is_read_only_from_the_literal_true() -> None:
    assert _parse(_payload(pull_request={"merged": True})).merged is True
    assert _parse(_payload(pull_request={"merged": None})).merged is False


def test_short_head_sha_is_seven_characters() -> None:
    assert _parse(_payload()).short_head_sha == HEAD_SHA[:7]


# -- Rejections ----------------------------------------------------------------------


@pytest.mark.parametrize("action", ["closed", "labeled", "edited", "ready_for_review", ""])
def test_unknown_actions_are_rejected(action: str) -> None:
    payload = _payload(action=action)

    with pytest.raises(WebhookPayloadError, match="unsupported pull_request action"):
        parse_pull_request_event(payload, event_type=PULL_REQUEST_EVENT_TYPE, action=action)


@pytest.mark.parametrize("event_type", ["push", "workflow_run", "pull_request_review", ""])
def test_unsupported_event_types_are_rejected(event_type: str) -> None:
    with pytest.raises(WebhookPayloadError, match="unsupported webhook event"):
        parse_pull_request_event(_payload(), event_type=event_type, action="opened")


def test_action_header_disagreeing_with_the_body_is_rejected() -> None:
    with pytest.raises(WebhookPayloadError, match="disagrees with the payload body"):
        _parse(_payload(action="opened"), action="reopened")


@pytest.mark.parametrize("sha", [None, "", "   "])
def test_missing_or_empty_head_sha_is_rejected(sha: Any) -> None:
    payload = _payload(pull_request={"head": {"ref": "feature", "sha": sha}})

    with pytest.raises(WebhookPayloadError, match=r"pull_request\.head\.sha"):
        _parse(payload)


@pytest.mark.parametrize("sha", [None, "", "   "])
def test_missing_or_empty_base_sha_is_rejected(sha: Any) -> None:
    payload = _payload(pull_request={"base": {"ref": "main", "sha": sha}})

    with pytest.raises(WebhookPayloadError, match=r"pull_request\.base\.sha"):
        _parse(payload)


def test_missing_head_object_is_rejected() -> None:
    payload = _payload()
    del payload["pull_request"]["head"]

    with pytest.raises(WebhookPayloadError, match=r"pull_request\.head\.sha"):
        _parse(payload)


def test_missing_pull_request_object_is_rejected() -> None:
    payload = _payload()
    del payload["pull_request"]

    with pytest.raises(WebhookPayloadError, match="'pull_request' object"):
        _parse(payload)


def test_missing_repository_object_is_rejected() -> None:
    payload = _payload()
    del payload["repository"]

    with pytest.raises(WebhookPayloadError, match="'repository' object"):
        _parse(payload)


@pytest.mark.parametrize("number", [None, 0, -1, "42", True])
def test_unusable_pull_request_number_is_rejected(number: Any) -> None:
    payload = _payload(number=number, pull_request={"number": number})

    with pytest.raises(WebhookPayloadError, match="pull request number"):
        _parse(payload)


def test_repository_without_any_identity_is_rejected() -> None:
    payload = _payload(repository={"id": 1, "owner": {}, "name": ""})

    with pytest.raises(WebhookPayloadError, match="repository"):
        _parse(payload)


def test_invalid_repository_full_name_is_rejected_as_a_payload_error() -> None:
    payload = _payload(repository={"full_name": "not-a-full-name"})

    with pytest.raises(WebhookPayloadError, match="not a usable repository reference"):
        _parse(payload)


def test_parser_does_not_mutate_the_payload() -> None:
    payload = _payload()
    original = deepcopy(payload)

    _parse(payload)

    assert payload == original


# -- Cheap filtering -----------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(SUPPORTED_PULL_REQUEST_ACTIONS))
def test_supported_actions_are_processed(action: str) -> None:
    assert should_process(PULL_REQUEST_EVENT_TYPE, action) is True


@pytest.mark.parametrize(
    ("event_type", "action"),
    [
        ("push", "opened"),
        ("workflow_run", "completed"),
        ("pull_request", "closed"),
        ("pull_request", "labeled"),
        ("pull_request", ""),
        (None, "opened"),
        ("pull_request", None),
        (None, None),
    ],
)
def test_irrelevant_deliveries_are_ignored(event_type: str | None, action: str | None) -> None:
    assert should_process(event_type, action) is False


def test_supported_actions_match_spec_section_10() -> None:
    assert {"opened", "synchronize", "reopened"} == SUPPORTED_PULL_REQUEST_ACTIONS


def test_planned_event_types_are_documented() -> None:
    assert set(PLANNED_EVENT_TYPES) == {"push", "workflow_run", "pull_request.closed"}
