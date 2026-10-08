"""GitHub webhook signature verification and pull-request payload parsing (spec section 10).

Supported today: the ``pull_request`` event with the actions ``opened``,
``synchronize``, and ``reopened``. One module owns both halves — signature verification
and payload parsing — because they are two checks on the same untrusted delivery and
splitting them invites a caller that does only one.

Deliberately **not** handled yet, with the reason each was deferred:

* ``push`` — a push to a branch with no open pull request has nothing to verify, and for
  a branch with an open pull request the ``pull_request`` event already carries the same
  head SHA (spec section 13 requires the analysed commit to be pinned, and the PR head is
  the commit under review). Adding it now would create duplicate jobs for one change.
* ``workflow_run`` — CI results are fetched through the GitHub API when a stage needs
  them. Making job admission depend on a workflow Scout does not control would couple
  verification to a customer's CI configuration.
* ``pull_request`` ``closed`` — Scout reports on changes under review. A closed PR has no
  further review to inform, and the commit that would have to be verified after a merge
  is the merge commit, not the PR head. When post-merge verification is wanted it will be
  a distinct event handler with its own pinned-commit rule.

GitHub's real payloads have quirks this module tolerates explicitly rather than by
``dict["key"]`` access: ``draft`` is absent on older events, ``body`` is ``null`` for an
empty description, ``installation`` is absent on some deliveries, and ``before``/``after``
are absent unless the event was triggered by a push to the PR branch.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any, Final

from pydantic import Field, ValidationError

from scout_domain.models.base import ScoutModel
from scout_domain.models.repository import RepositoryRef
from scout_github.errors import WebhookPayloadError, WebhookSignatureError

__all__ = [
    "PLANNED_EVENT_TYPES",
    "PULL_REQUEST_EVENT_TYPE",
    "SIGNATURE_PREFIX",
    "SUPPORTED_PULL_REQUEST_ACTIONS",
    "WEBHOOK_DELIVERY_HEADER",
    "WEBHOOK_EVENT_HEADER",
    "WEBHOOK_SIGNATURE_HEADER",
    "PullRequestEvent",
    "parse_pull_request_event",
    "should_process",
    "verify_webhook_signature",
]

#: Headers GitHub sets on every delivery. Named here so the API layer never spells them
#: by hand: a typo in a header name is a silent security hole when the result is "no
#: signature to check".
WEBHOOK_SIGNATURE_HEADER: Final = "X-Hub-Signature-256"
WEBHOOK_EVENT_HEADER: Final = "X-GitHub-Event"
WEBHOOK_DELIVERY_HEADER: Final = "X-GitHub-Delivery"

#: The only signature algorithm GitHub sends today, and the only one Scout accepts.
SIGNATURE_PREFIX: Final = "sha256="

PULL_REQUEST_EVENT_TYPE: Final = "pull_request"

#: Actions that start verification work (spec section 10).
SUPPORTED_PULL_REQUEST_ACTIONS: Final[frozenset[str]] = frozenset(
    {"opened", "synchronize", "reopened"}
)

#: Events Scout intends to handle in a later milestone. Exported so the API's 202/ignored
#: response can explain itself instead of looking like a bug. See the module docstring.
PLANNED_EVENT_TYPES: Final[tuple[str, ...]] = (
    "push",
    "workflow_run",
    "pull_request.closed",
)

_MAX_VALIDATION_ERRORS: Final = 10


class PullRequestEvent(ScoutModel):
    """A validated ``pull_request`` delivery, ready to become a verification job.

    The commit SHAs are first-class fields because spec section 13 makes them
    load-bearing: a job is only meaningful if it names the exact base and head it will
    analyse, and every later stage re-reads them from here.

    ``installation_id`` is a credential handle, not data: it selects which GitHub App
    installation's token may be used (spec sections 54 and 55), and it must never be
    placed in agent context.
    """

    action: str = Field(min_length=1, max_length=50)
    #: Set by the caller from the ``X-GitHub-Delivery`` header, not by the parser. The
    #: delivery id is the idempotency key for job creation (spec section 42) and it only
    #: exists as a header, so keeping it out of the parser keeps the parser pure.
    delivery_id: str | None = Field(default=None, max_length=100)

    #: ``None`` when the delivery carries no installation block. The API layer must treat
    #: that as "cannot authenticate", never as installation 0.
    installation_id: int | None = Field(default=None, ge=1)

    repository: RepositoryRef
    pull_request_number: int = Field(ge=1)

    title: str = ""
    #: ``None`` when GitHub sent ``null``; an empty description is not an empty string.
    body: str | None = None
    author: str = ""
    html_url: str = ""

    #: Read as ``event.draft is True``: the payload may omit the field entirely.
    draft: bool = False
    merged: bool = False

    base_sha: str = Field(min_length=1, max_length=64)
    head_sha: str = Field(min_length=1, max_length=64)
    base_ref: str = ""
    head_ref: str = ""

    #: The branch tip before and after the push that triggered this event. Absent on
    #: ``opened``/``reopened`` because no push caused them.
    before_sha: str | None = Field(default=None, max_length=64)
    after_sha: str | None = Field(default=None, max_length=64)

    @property
    def short_head_sha(self) -> str:
        """First seven characters of the head SHA, for logs and check titles."""
        return self.head_sha[:7]


def verify_webhook_signature(
    *,
    payload: bytes,
    signature_header: str | None,
    secret: str,
) -> None:
    """Verify GitHub's HMAC-SHA256 signature over the raw request body.

    Three decisions, each of which is a known way this check is defeated:

    * **Raw bytes, never re-serialised JSON.** GitHub signs the exact octets it sent.
      Re-encoding the parsed body (different key order, spacing, or unicode escaping)
      yields a different digest, so the signature must be computed over the bytes as
      received. This is why the parameter is ``bytes`` and not a mapping.
    * **``hmac.compare_digest``.** An ordinary ``==`` comparison returns on the first
      differing character, leaking how much of a guessed digest is correct and making a
      byte-by-byte forgery feasible.
    * **A missing secret is fatal.** Treating "no secret configured" as "skip
      verification" lets anyone who can reach the endpoint start jobs. Failing loudly is
      the only safe reading of incomplete configuration (spec section 55).

    Args:
        payload: The raw request body exactly as received.
        signature_header: The ``X-Hub-Signature-256`` header value, if present.
        secret: The configured webhook secret.

    Raises:
        WebhookSignatureError: When the secret is unset, the header is missing or
            malformed, or the digest does not match.
    """
    if not secret or not secret.strip():
        raise WebhookSignatureError(
            "no GitHub webhook secret is configured; refusing to accept a delivery "
            "whose signature cannot be verified (set SCOUT_GITHUB_WEBHOOK_SECRET)"
        )
    if signature_header is None or not signature_header.strip():
        raise WebhookSignatureError(
            f"missing {WEBHOOK_SIGNATURE_HEADER} header; the delivery is unsigned"
        )

    provided = signature_header.strip()
    if not provided.startswith(SIGNATURE_PREFIX):
        raise WebhookSignatureError(
            f"malformed signature header: expected a {SIGNATURE_PREFIX!r} prefix "
            "(GitHub's older sha1 signature is not accepted)"
        )

    provided_digest = provided[len(SIGNATURE_PREFIX) :].lower()
    expected_digest = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    try:
        matches = hmac.compare_digest(expected_digest, provided_digest)
    except TypeError as exc:
        # compare_digest rejects non-ASCII strings; a digest is hexadecimal by
        # definition, so this is a malformed header rather than a mismatch.
        raise WebhookSignatureError(
            "malformed signature header: the digest is not hexadecimal"
        ) from exc

    if not matches:
        raise WebhookSignatureError("webhook signature does not match the request body")


def should_process(event_type: str | None, action: str | None) -> bool:
    """Return ``True`` when a delivery is worth parsing.

    The API layer calls this before touching the body so that irrelevant deliveries
    (``push``, a ``pull_request`` ``labeled`` action, a bot's ``edited`` churn) cost one
    string comparison instead of a full parse and a job attempt.

    ``None`` is accepted for both arguments because HTTP header lookups return ``None``
    when a header is absent; a missing header is a delivery to ignore, not a crash.

    Args:
        event_type: Value of ``X-GitHub-Event``.
        action: Value of the payload's ``action`` field, or the ``action`` the caller
            read from the body.
    """
    if event_type is None or action is None:
        return False
    return (
        event_type.strip() == PULL_REQUEST_EVENT_TYPE
        and action.strip() in SUPPORTED_PULL_REQUEST_ACTIONS
    )


def parse_pull_request_event(
    payload: dict[str, Any],
    *,
    event_type: str,
    action: str,
) -> PullRequestEvent:
    """Validate a ``pull_request`` delivery and project it into :class:`PullRequestEvent`.

    GitHub payloads are untrusted input: they are signed, which proves origin, not
    schema. Every field Scout depends on is therefore validated explicitly, and anything
    missing or unusable raises :class:`WebhookPayloadError` instead of producing a job
    that quietly verifies the wrong commit.

    Args:
        payload: The parsed JSON body.
        event_type: ``X-GitHub-Event`` header value.
        action: The event action, typically the payload's own ``action``.

    Returns:
        The validated event. ``delivery_id`` is ``None``; the caller sets it from the
        ``X-GitHub-Delivery`` header.

    Raises:
        WebhookPayloadError: When the event type or action is unsupported, the body
            disagrees with the header, a required object is missing, or a SHA is absent
            or empty.
    """
    normalized_action = _require_supported_action(event_type, action)

    payload_action = payload.get("action")
    if isinstance(payload_action, str) and payload_action.strip() != normalized_action:
        # The header and the body disagreeing is a real failure mode (a proxy replaying
        # a body under a different header). Trusting either one silently could verify an
        # action the caller never intended to process.
        raise WebhookPayloadError(
            "the action header disagrees with the payload body; refusing to guess "
            "which one is authoritative",
            header_action=normalized_action,
            payload_action=payload_action,
        )

    pull_request = _require_mapping(payload.get("pull_request"), field="pull_request")
    repository_payload = _require_mapping(payload.get("repository"), field="repository")

    base = _optional_mapping(pull_request.get("base"))
    head = _optional_mapping(pull_request.get("head"))
    base_sha = _require_sha(base.get("sha"), field="pull_request.base.sha")
    head_sha = _require_sha(head.get("sha"), field="pull_request.head.sha")

    try:
        return PullRequestEvent(
            action=normalized_action,
            installation_id=_extract_installation_id(payload),
            repository=_repository_ref(repository_payload),
            pull_request_number=_pull_request_number(payload, pull_request),
            title=_optional_str(pull_request.get("title")) or "",
            body=_nullable_str(pull_request.get("body")),
            author=_login(_optional_mapping(pull_request.get("user"))),
            html_url=_optional_str(pull_request.get("html_url")) or "",
            # Only the literal boolean ``true`` counts. A truthy string such as "false"
            # would otherwise mark an open PR as a draft and suppress verification.
            draft=pull_request.get("draft") is True,
            merged=pull_request.get("merged") is True,
            base_sha=base_sha,
            head_sha=head_sha,
            base_ref=_optional_str(base.get("ref")) or "",
            head_ref=_optional_str(head.get("ref")) or "",
            before_sha=_optional_str(payload.get("before")),
            after_sha=_optional_str(payload.get("after")),
        )
    except ValidationError as exc:
        raise WebhookPayloadError(
            "pull_request payload failed validation",
            errors=[
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:_MAX_VALIDATION_ERRORS]
            ],
        ) from exc


def _require_supported_action(event_type: str, action: str) -> str:
    """Validate the event type and action, returning the normalised action."""
    if event_type.strip() != PULL_REQUEST_EVENT_TYPE:
        raise WebhookPayloadError(
            f"unsupported webhook event {event_type!r}; Scout only handles "
            f"{PULL_REQUEST_EVENT_TYPE!r} today",
            event_type=event_type,
            planned_events=list(PLANNED_EVENT_TYPES),
        )

    normalized_action = action.strip()
    if normalized_action not in SUPPORTED_PULL_REQUEST_ACTIONS:
        raise WebhookPayloadError(
            f"unsupported pull_request action {normalized_action!r}; refusing to guess "
            "how to handle it",
            action=normalized_action,
            supported_actions=sorted(SUPPORTED_PULL_REQUEST_ACTIONS),
        )
    return normalized_action


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    """Return ``value`` when it is a JSON object, otherwise raise."""
    if not isinstance(value, Mapping):
        raise WebhookPayloadError(
            f"pull_request event has no {field!r} object",
            field=field,
        )
    return value


def _optional_mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` when it is a JSON object, otherwise an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def _require_sha(value: Any, *, field: str) -> str:
    """Return a non-empty SHA string, or raise.

    Scout refuses to proceed without both SHAs: analysing a moving branch instead of a
    pinned commit is the failure spec section 13 calls out explicitly.
    """
    if not isinstance(value, str) or not value.strip():
        raise WebhookPayloadError(
            f"{field} is missing or empty; Scout cannot pin the commit it would verify",
            field=field,
        )
    return value.strip()


def _optional_str(value: Any) -> str | None:
    """Return a stripped string, or ``None`` for absent/empty/non-string values."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _nullable_str(value: Any) -> str | None:
    """Return a stripped string, preserving ``None`` for a null body."""
    if value is None:
        return None
    return _optional_str(value)


def _login(user: Mapping[str, Any]) -> str:
    """Return a user's login, or ``""`` when the account was deleted (``ghost``)."""
    return _optional_str(user.get("login")) or ""


def _extract_installation_id(payload: Mapping[str, Any]) -> int | None:
    """Read ``installation.id`` when present.

    Absent on some deliveries (organisation-level and legacy events). Returning ``None``
    is intentional: the API layer must fail with an actionable "no installation to
    authenticate as" error rather than attempt installation 0.
    """
    installation = payload.get("installation")
    if not isinstance(installation, Mapping):
        return None
    candidate = installation.get("id")
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 1:
        return None
    return candidate


def _pull_request_number(
    payload: Mapping[str, Any],
    pull_request: Mapping[str, Any],
) -> int:
    """Read the pull request number from the payload's top level, else the PR object."""
    raw = payload.get("number", pull_request.get("number"))
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise WebhookPayloadError(
            "pull_request event has no usable pull request number",
            field="number",
        )
    return raw


def _repository_ref(repository: Mapping[str, Any]) -> RepositoryRef:
    """Build a :class:`RepositoryRef` from the payload's repository object."""
    full_name = _optional_str(repository.get("full_name"))
    if full_name is None:
        # Enterprise Server payloads and some legacy deliveries omit full_name while
        # still providing owner.login and name; reconstruct rather than reject.
        owner = _login(_optional_mapping(repository.get("owner")))
        name = _optional_str(repository.get("name"))
        if owner and name:
            full_name = f"{owner}/{name}"

    if full_name is None:
        raise WebhookPayloadError(
            "repository payload has no full_name (and no owner/name pair)",
            field="repository.full_name",
        )

    raw_id = repository.get("id")
    github_id = (
        raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id >= 1 else None
    )

    try:
        return RepositoryRef(
            full_name=full_name,
            github_id=github_id,
            default_branch=_optional_str(repository.get("default_branch")) or "main",
        )
    except ValidationError as exc:
        raise WebhookPayloadError(
            "repository payload is not a usable repository reference",
            errors=[
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()[:_MAX_VALIDATION_ERRORS]
            ],
        ) from exc
