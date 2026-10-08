"""Requirement material for feature verification (spec section 24).

Before Scout can verify that a change does what was asked, it needs the *asking*: the
pull-request description, the issues the PR claims to close, and the commit messages.
This module collects that material and nothing else.

Why it is a pure function: acceptance-criteria extraction is only as trustworthy as its
input, and the input's ordering and de-noising must be testable without GitHub, without
a database, and without a model. The rules encoded here are deliberately conservative —
dropping a genuine requirement would silently weaken verification, which is worse than
leaving a line of boilerplate in.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

__all__ = ["MAX_REQUIREMENT_TEXT_CHARS", "extract_requirement_text"]

#: Cap on the assembled text. Acceptance criteria are extracted by a model, and the
#: context budget is shared with the diff and repository summary; a 100 KB PR
#: description must not push the evidence out of the window. Anything dropped is
#: announced in the output rather than lost silently.
MAX_REQUIREMENT_TEXT_CHARS: Final = 20_000

#: HTML comments are template instructions to the author ("describe the change here"),
#: not requirements. Non-greedy and DOTALL so multi-line comments are removed entirely.
_HTML_COMMENT_PATTERN: Final = re.compile(r"<!--.*?-->", re.DOTALL)

#: Runs of blank lines, collapsed so that removed comment blocks do not leave holes.
_BLANK_RUN_PATTERN: Final = re.compile(r"\n{3,}")

#: Boilerplate that tooling adds and that no acceptance criterion comes from. Each
#: pattern matches a whole line and is anchored, so ordinary prose containing these
#: words is untouched. The dependabot rule requires a linked dependency name, which is
#: how dependabot always writes it, so a human sentence such as "update the API from v1
#: to v2" is preserved.
_BOT_NOISE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\s*signed-off-by:.*$", re.IGNORECASE),
    re.compile(r"^\s*co-authored-by:.*$", re.IGNORECASE),
    re.compile(
        r"^\s*bumps?\s+\[[^\]]+\]\([^)]*\)\s+from\s+\S+\s+to\s+\S+.*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:#{1,6}\s*)?(?:release notes|full changelog|changelog)\s*:?\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*</?(?:details|summary)[^>]*>.*$", re.IGNORECASE),
    re.compile(
        r"^\s*\[?@?(?:dependabot|renovate|github-actions|codecov)(?:\[bot\])?\]?\s*$",
        re.IGNORECASE,
    ),
)

_PR_BODY_HEADING: Final = "## Pull request description"
_ISSUE_HEADING: Final = "## Linked issue"
_COMMITS_HEADING: Final = "## Commit messages"

_TRUNCATION_NOTICE_TEMPLATE: Final = "\n\n[requirement text truncated at {limit} characters]"


def extract_requirement_text(
    pull_request_body: str | None,
    linked_issue_bodies: Sequence[str | None],
    commit_messages: Sequence[str],
) -> str:
    """Collect the material acceptance criteria are extracted from.

    Ordering is meaningful and preserved: the pull-request description comes first
    because it is the author's own statement of intent, then the linked issues in the
    order the caller resolved them, then commit messages. When two sources disagree the
    earlier one is the more authoritative claim, and an extractor that sees them in this
    order can weigh them accordingly.

    Args:
        pull_request_body: The PR description; GitHub may send ``None``.
        linked_issue_bodies: Bodies of the issues the PR closes, in resolution order.
            Individual entries may be ``None`` when an issue has no body.
        commit_messages: Commit messages, oldest first is conventional but not required.
            Merges repeat messages, so duplicates are dropped while order is preserved.

    Returns:
        Markdown-ish prose with provenance headings, at most
        :data:`MAX_REQUIREMENT_TEXT_CHARS` characters. An empty string when there is no
        usable material — callers must treat that as "no requirements known", never as
        "no requirements".
    """
    sections: list[str] = []

    cleaned_pr_body = _clean(pull_request_body)
    if cleaned_pr_body:
        sections.append(f"{_PR_BODY_HEADING}\n{cleaned_pr_body}")

    for index, issue_body in enumerate(linked_issue_bodies, start=1):
        cleaned_issue = _clean(issue_body)
        if cleaned_issue:
            sections.append(f"{_ISSUE_HEADING} {index}\n{cleaned_issue}")

    unique_messages = _unique_non_empty(commit_messages)
    if unique_messages:
        bullets = "\n".join(f"- {_single_line(message)}" for message in unique_messages)
        sections.append(f"{_COMMITS_HEADING}\n{bullets}")

    if not sections:
        return ""
    return _cap("\n\n".join(sections))


def _clean(text: str | None) -> str:
    """Strip HTML comments and tooling boilerplate, then normalise whitespace."""
    if text is None:
        return ""

    without_comments = _HTML_COMMENT_PATTERN.sub("", text)
    kept_lines = [
        line.rstrip() for line in without_comments.splitlines() if not _is_bot_noise(line)
    ]
    collapsed = _BLANK_RUN_PATTERN.sub("\n\n", "\n".join(kept_lines))
    return collapsed.strip()


def _is_bot_noise(line: str) -> bool:
    """Return ``True`` when a line is tooling boilerplate rather than a requirement."""
    return any(pattern.match(line) is not None for pattern in _BOT_NOISE_PATTERNS)


def _unique_non_empty(values: Sequence[str]) -> list[str]:
    """Return cleaned commit messages without duplicates, preserving first-seen order."""
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = _clean(value)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)
    return unique


def _single_line(message: str) -> str:
    """Collapse a commit message onto one bullet line, keeping its subject and body."""
    return " ".join(part.strip() for part in message.splitlines() if part.strip())


def _cap(text: str) -> str:
    """Truncate deterministically at :data:`MAX_REQUIREMENT_TEXT_CHARS`, announcing it."""
    if len(text) <= MAX_REQUIREMENT_TEXT_CHARS:
        return text

    notice = _TRUNCATION_NOTICE_TEMPLATE.format(limit=MAX_REQUIREMENT_TEXT_CHARS)
    budget = MAX_REQUIREMENT_TEXT_CHARS - len(notice)
    if budget <= 0:  # pragma: no cover - defensive; the notice is far shorter
        return text[:MAX_REQUIREMENT_TEXT_CHARS]
    return text[:budget].rstrip() + notice
