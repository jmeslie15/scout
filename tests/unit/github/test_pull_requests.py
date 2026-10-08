"""Requirement-material extraction (spec section 24)."""

from __future__ import annotations

from scout_github.pull_requests import MAX_REQUIREMENT_TEXT_CHARS, extract_requirement_text

PR_BODY = "Adds acceptance criteria to the verification stage."
ISSUE_BODY = "The checker must report NOT_VERIFIED when no procedure ran."
COMMIT = "Add criterion checker"


def test_all_empty_inputs_produce_an_empty_string() -> None:
    assert extract_requirement_text(None, [], []) == ""
    assert extract_requirement_text("   ", [None, "  "], ["", "  "]) == ""


def test_pull_request_body_is_included_under_a_provenance_heading() -> None:
    text = extract_requirement_text(PR_BODY, [], [])

    assert text == f"## Pull request description\n{PR_BODY}"


def test_pr_body_comes_first_then_issues_then_commits() -> None:
    text = extract_requirement_text(PR_BODY, [ISSUE_BODY], [COMMIT])

    assert text.index("## Pull request description") < text.index("## Linked issue 1")
    assert text.index("## Linked issue 1") < text.index("## Commit messages")


def test_linked_issues_keep_the_callers_order() -> None:
    text = extract_requirement_text(None, ["First issue", "Second issue"], [])

    assert text.index("First issue") < text.index("Second issue")
    assert "## Linked issue 1" in text
    assert "## Linked issue 2" in text


def test_none_issue_bodies_are_skipped() -> None:
    text = extract_requirement_text(None, [None, "Only this one", None], [])

    assert "Only this one" in text
    # Numbering follows the caller's list, so the surviving entry keeps its position
    # (it was the second) rather than being renumbered as the first.
    assert "## Linked issue 2\n" in text
    assert "## Linked issue 1" not in text


def test_commit_messages_are_bulleted_on_one_line_each() -> None:
    text = extract_requirement_text(None, [], ["Add checker\n\nLonger explanation."])

    assert "- Add checker Longer explanation." in text


def test_duplicate_commit_messages_are_dropped_but_order_is_kept() -> None:
    text = extract_requirement_text(
        None, [], ["Merge branch 'main'", COMMIT, "Merge branch 'main'"]
    )

    assert text.count("- Merge branch 'main'") == 1
    assert text.index("Merge branch") < text.index(COMMIT)


# -- De-noising ----------------------------------------------------------------------


def test_html_comments_are_stripped() -> None:
    body = "Real requirement.\n<!-- reviewers: fill this in -->\nAnother requirement."

    text = extract_requirement_text(body, [], [])

    assert "reviewers" not in text
    assert "Real requirement." in text
    assert "Another requirement." in text


def test_multiline_html_comments_are_stripped() -> None:
    body = "Before.\n<!--\nA whole block of template\ninstructions\n-->\nAfter."

    text = extract_requirement_text(body, [], [])

    assert "template" not in text
    assert "Before." in text
    assert "After." in text


def test_comment_removal_does_not_leave_blank_holes() -> None:
    body = "Before.\n\n<!-- gone -->\n\n\n\nAfter."

    text = extract_requirement_text(body, [], [])

    assert "\n\n\n" not in text


def test_tooling_trailers_are_dropped() -> None:
    body = (
        "Real body.\n"
        "Signed-off-by: Author <author@example.com>\n"
        "Co-authored-by: Other <other@example.com>\n"
    )

    text = extract_requirement_text(body, [], [])

    assert "Signed-off-by" not in text
    assert "Co-authored-by" not in text
    assert "Real body." in text


def test_dependabot_boilerplate_is_dropped() -> None:
    body = (
        "Bumps [some-lib](https://github.com/x/y) from 1.0.0 to 1.0.1.\n"
        "<details>\n"
        "<summary>Release notes</summary>\n"
        "</details>\n"
        "dependabot[bot]\n"
        "**Full Changelog**: https://github.com/x/y/compare/v1.0.0...v1.0.1\n"
    )

    text = extract_requirement_text(body, [], [])

    assert "Bumps" not in text
    assert "<details>" not in text
    assert "<summary>" not in text
    assert "dependabot[bot]" not in text


def test_human_sentences_that_look_like_bumps_are_preserved() -> None:
    body = "Update the API from v1 to v2 across all callers."

    text = extract_requirement_text(body, [], [])

    assert body in text


def test_body_that_is_only_boilerplate_yields_no_section() -> None:
    text = extract_requirement_text("Signed-off-by: Someone <s@example.com>", [], [])

    assert text == ""


# -- Length cap ----------------------------------------------------------------------


def test_output_is_capped_with_a_documented_notice() -> None:
    body = "requirement line\n" * 5000

    text = extract_requirement_text(body, [], [])

    assert len(text) <= MAX_REQUIREMENT_TEXT_CHARS
    assert "requirement text truncated at" in text


def test_output_at_the_limit_is_not_truncated() -> None:
    body = "x" * (MAX_REQUIREMENT_TEXT_CHARS - len("## Pull request description\n"))

    text = extract_requirement_text(body, [], [])

    assert len(text) <= MAX_REQUIREMENT_TEXT_CHARS
    assert "truncated" not in text


def test_cap_is_deterministic() -> None:
    body = "requirement line\n" * 5000

    assert extract_requirement_text(body, [], []) == extract_requirement_text(body, [], [])


def test_none_body_with_usable_issues_still_produces_text() -> None:
    text = extract_requirement_text(None, [ISSUE_BODY], [])

    assert ISSUE_BODY in text
    assert "## Pull request description" not in text
