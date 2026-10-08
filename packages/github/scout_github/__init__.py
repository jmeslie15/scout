"""GitHub integration for Scout (spec sections 9, 10, 45, 51, 54, 55).

This package is the *only* component that holds GitHub App credentials, verifies
webhook signatures, and writes to GitHub. Everything it exposes is deliberately
narrow:

* :class:`~scout_github.client.GitHubClient` — the interface the rest of the platform
  programs against, with an httpx implementation for production and an in-memory
  implementation used when no GitHub App is configured (the shipped default, so the
  whole platform is exercisable offline).
* :class:`~scout_github.auth.GitHubAppAuthenticator` — app JWTs and cached installation
  tokens, with per-installation locking so a burst of jobs causes one token request.
* :func:`~scout_github.webhooks.verify_webhook_signature` and
  :func:`~scout_github.webhooks.parse_pull_request_event` — HMAC verification over the
  raw body and validated projection of ``pull_request`` deliveries.
* :func:`~scout_github.checks.render_check_summary` and
  :func:`~scout_github.checks.build_check_run_payload` — the check run that carries
  Scout's conclusion onto the pull request.
* :func:`~scout_github.diff.build_change_set` and
  :func:`~scout_github.diff.render_unified_diff` — GitHub's diff vocabulary translated
  into Scout's pinned change set, plus the raw unified diff stored as evidence.
* :func:`~scout_github.pull_requests.extract_requirement_text` — the material acceptance
  criteria are extracted from.

Importing this package registers the retry classification of every error type it defines
(spec section 51), so the orchestrator can classify failures without importing
``scout_domain.state.retry`` directly.
"""

from __future__ import annotations

from scout_github.auth import (
    ENV_API_URL,
    ENV_APP_ID,
    ENV_PRIVATE_KEY,
    ENV_PRIVATE_KEY_PATH,
    INSTALLATION_TOKEN_REFRESH_MARGIN_SECONDS,
    MAX_JWT_LIFETIME_SECONDS,
    MAX_JWT_TTL_SECONDS,
    GitHubAppAuthenticator,
    InstallationTokenProvider,
    create_app_jwt,
    load_private_key,
)
from scout_github.checks import (
    CHECK_CONCLUSION_BY_RECOMMENDATION,
    CHECK_RUN_NAME,
    MAX_CHECK_SUMMARY_CHARS,
    CheckRunConclusion,
    CheckRunStatus,
    build_check_run_payload,
    conclusion_for_recommendation,
    job_dashboard_url,
    render_check_summary,
    resolve_check_state,
    truncate_check_summary,
)
from scout_github.client import (
    DEFAULT_FAKE_BASE_SHA,
    DEFAULT_FAKE_HEAD_SHA,
    FakeGitHubClient,
    GitHubClient,
    GitHubCommentRecord,
    HttpGitHubClient,
)
from scout_github.diff import (
    GITHUB_STATUS_TO_CHANGE_TYPE,
    build_change_set,
    change_type_for_status,
    is_binary_file,
    render_unified_diff,
    to_changed_file,
)

# Errors first: importing this module is what registers the failure classes.
from scout_github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTransientError,
    WebhookPayloadError,
    WebhookSignatureError,
)
from scout_github.models import (
    GitHubBranchRef,
    GitHubCheckOutput,
    GitHubCheckRun,
    GitHubCommit,
    GitHubCompare,
    GitHubFile,
    GitHubFileContents,
    GitHubIssue,
    GitHubIssueComment,
    GitHubModel,
    GitHubPullRequest,
    GitHubRepository,
    GitHubUser,
)
from scout_github.pull_requests import (
    MAX_REQUIREMENT_TEXT_CHARS,
    extract_requirement_text,
)
from scout_github.webhooks import (
    PLANNED_EVENT_TYPES,
    PULL_REQUEST_EVENT_TYPE,
    SIGNATURE_PREFIX,
    SUPPORTED_PULL_REQUEST_ACTIONS,
    WEBHOOK_DELIVERY_HEADER,
    WEBHOOK_EVENT_HEADER,
    WEBHOOK_SIGNATURE_HEADER,
    PullRequestEvent,
    parse_pull_request_event,
    should_process,
    verify_webhook_signature,
)

__version__ = "0.1.0"

__all__ = [
    "CHECK_CONCLUSION_BY_RECOMMENDATION",
    "CHECK_RUN_NAME",
    "DEFAULT_FAKE_BASE_SHA",
    "DEFAULT_FAKE_HEAD_SHA",
    "ENV_API_URL",
    "ENV_APP_ID",
    "ENV_PRIVATE_KEY",
    "ENV_PRIVATE_KEY_PATH",
    "GITHUB_STATUS_TO_CHANGE_TYPE",
    "INSTALLATION_TOKEN_REFRESH_MARGIN_SECONDS",
    "MAX_CHECK_SUMMARY_CHARS",
    "MAX_JWT_LIFETIME_SECONDS",
    "MAX_JWT_TTL_SECONDS",
    "MAX_REQUIREMENT_TEXT_CHARS",
    "PLANNED_EVENT_TYPES",
    "PULL_REQUEST_EVENT_TYPE",
    "SIGNATURE_PREFIX",
    "SUPPORTED_PULL_REQUEST_ACTIONS",
    "WEBHOOK_DELIVERY_HEADER",
    "WEBHOOK_EVENT_HEADER",
    "WEBHOOK_SIGNATURE_HEADER",
    "CheckRunConclusion",
    "CheckRunStatus",
    "FakeGitHubClient",
    "GitHubAppAuthenticator",
    "GitHubAuthError",
    "GitHubBranchRef",
    "GitHubCheckOutput",
    "GitHubCheckRun",
    "GitHubClient",
    "GitHubCommentRecord",
    "GitHubCommit",
    "GitHubCompare",
    "GitHubError",
    "GitHubFile",
    "GitHubFileContents",
    "GitHubIssue",
    "GitHubIssueComment",
    "GitHubModel",
    "GitHubNotFoundError",
    "GitHubPullRequest",
    "GitHubRateLimitError",
    "GitHubRepository",
    "GitHubTransientError",
    "GitHubUser",
    "HttpGitHubClient",
    "InstallationTokenProvider",
    "PullRequestEvent",
    "WebhookPayloadError",
    "WebhookSignatureError",
    "__version__",
    "build_change_set",
    "build_check_run_payload",
    "change_type_for_status",
    "conclusion_for_recommendation",
    "create_app_jwt",
    "extract_requirement_text",
    "is_binary_file",
    "job_dashboard_url",
    "load_private_key",
    "parse_pull_request_event",
    "render_check_summary",
    "render_unified_diff",
    "resolve_check_state",
    "should_process",
    "to_changed_file",
    "truncate_check_summary",
    "verify_webhook_signature",
]
