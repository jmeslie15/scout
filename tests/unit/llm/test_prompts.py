"""Prompt loading, digests, strict rendering, and version ordering (spec section 39)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from scout_domain.enums import AgentRole
from scout_llm.errors import PromptNotFoundError, PromptRenderError
from scout_llm.prompts import PromptLibrary

BODY = "Review the diff:\n$diff\n"
ROLE = AgentRole.CODE_REVIEWER


def test_load_reads_the_role_and_version_path(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
    prompts_root: Path,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    template = prompt_library.load(ROLE, "v1")

    assert template.role is ROLE
    assert template.version == "v1"
    assert template.body == BODY
    assert (prompts_root / "code_reviewer" / "v1.md").is_file()


def test_sha256_is_of_the_unrendered_body_and_is_stable(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    first = prompt_library.load(ROLE, "v1")
    second = prompt_library.load(ROLE, "v1")

    assert first.sha256 == second.sha256
    assert first.sha256 == hashlib.sha256(BODY.encode("utf-8")).hexdigest()


def test_sha256_changes_when_the_body_changes(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    write_prompt(ROLE, "v2", BODY + "\nExtra instruction.\n")
    assert prompt_library.load(ROLE, "v1").sha256 != prompt_library.load(ROLE, "v2").sha256


def test_render_substitutes_every_placeholder(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    assert prompt_library.load(ROLE, "v1").render(diff="+ added line") == (
        "Review the diff:\n+ added line\n"
    )


def test_render_supports_braced_placeholders_and_literal_dollars(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", "Cost is $$5 for ${diff_size} tokens.")
    template = prompt_library.load(ROLE, "v1")
    assert template.placeholders == frozenset({"diff_size"})
    assert template.render(diff_size="100") == "Cost is $5 for 100 tokens."


def test_render_raises_on_a_missing_variable(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    with pytest.raises(PromptRenderError) as excinfo:
        prompt_library.load(ROLE, "v1").render()

    assert "missing values for ['diff']" in str(excinfo.value)
    assert excinfo.value.failure_class.value == "non_retryable"


def test_render_raises_on_an_unused_variable(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    """A typo'd variable name must fail loudly rather than send a prompt with a gap."""
    write_prompt(ROLE, "v1", BODY)
    with pytest.raises(PromptRenderError) as excinfo:
        prompt_library.load(ROLE, "v1").render(diff="x", dif="y")

    assert "unused variables ['dif']" in str(excinfo.value)


def test_render_reports_both_missing_and_unused_variables(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    with pytest.raises(PromptRenderError) as excinfo:
        prompt_library.load(ROLE, "v1").render(other="x")

    message = str(excinfo.value)
    assert "missing values for ['diff']" in message
    assert "unused variables ['other']" in message


def test_render_rejects_non_string_values(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    with pytest.raises(PromptRenderError):
        prompt_library.load(ROLE, "v1").render(diff=42)  # type: ignore[arg-type]


def test_malformed_placeholder_is_rejected_at_load_time(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", "The build cost $5 and the diff is $diff")
    with pytest.raises(PromptRenderError) as excinfo:
        prompt_library.load(ROLE, "v1")

    assert "malformed placeholders" in str(excinfo.value)


def test_braces_in_json_examples_are_literal_text(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    """``string.Template`` was chosen precisely so JSON examples need no escaping."""
    body = 'Answer with {"findings": []} and use the diff:\n$diff\n'
    write_prompt(ROLE, "v1", body)
    template = prompt_library.load(ROLE, "v1")

    assert template.placeholders == frozenset({"diff"})
    assert template.render(diff="D") == 'Answer with {"findings": []} and use the diff:\nD\n'


def test_versions_are_naturally_ordered(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    for version in ("v10", "v2", "v1", "v9"):
        write_prompt(ROLE, version, BODY)

    assert prompt_library.versions(ROLE) == ["v1", "v2", "v9", "v10"]


def test_versions_ignores_files_that_are_not_prompt_versions(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
    prompts_root: Path,
) -> None:
    """A README committed next to the prompts must never become ``latest()``."""
    write_prompt(ROLE, "v1", BODY)
    (prompts_root / ROLE.value / "notes.txt").write_text("ignore me", encoding="utf-8")
    (prompts_root / ROLE.value / "README.md").write_text("also ignore", encoding="utf-8")

    assert prompt_library.versions(ROLE) == ["v1"]


def test_versions_accepts_iso_date_and_dotted_versions(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    for version in ("v1", "v1.2", "2024-06-01"):
        write_prompt(ROLE, version, BODY)

    assert prompt_library.versions(ROLE) == ["v1", "v1.2", "2024-06-01"]


def test_loading_a_non_version_file_is_refused(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
    prompts_root: Path,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    (prompts_root / ROLE.value / "README.md").write_text("docs", encoding="utf-8")

    with pytest.raises(PromptNotFoundError):
        prompt_library.load(ROLE, "README")


def test_versions_of_an_unknown_role_is_empty(prompt_library: PromptLibrary) -> None:
    assert prompt_library.versions(AgentRole.SECURITY) == []


def test_latest_returns_the_highest_version(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    write_prompt(ROLE, "v10", BODY + "\nnewer\n")

    assert prompt_library.latest(ROLE).version == "v10"


def test_latest_with_no_versions_raises_with_an_actionable_message(
    prompt_library: PromptLibrary,
) -> None:
    with pytest.raises(PromptNotFoundError) as excinfo:
        prompt_library.latest(ROLE)

    assert "no prompt versions exist" in str(excinfo.value)


def test_missing_version_lists_available_versions(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    write_prompt(ROLE, "v2", BODY)

    with pytest.raises(PromptNotFoundError) as excinfo:
        prompt_library.load(ROLE, "v3")

    message = str(excinfo.value)
    assert "v1, v2" in message
    assert excinfo.value.context["available_versions"] == ["v1", "v2"]


def test_missing_prompts_root_raises_a_clear_error(tmp_path: Path) -> None:
    library = PromptLibrary(tmp_path / "absent")

    with pytest.raises(PromptNotFoundError) as excinfo:
        library.load(ROLE, "v1")

    assert "does not exist" in str(excinfo.value)


@pytest.mark.parametrize("version", ["../secrets", "v1/../../etc/passwd", "", "a" * 200 + "/x"])
def test_versions_cannot_escape_the_prompts_directory(
    prompt_library: PromptLibrary,
    version: str,
) -> None:
    with pytest.raises(PromptNotFoundError):
        prompt_library.load(ROLE, version)


def test_roles_lists_only_roles_with_a_directory(
    write_prompt: Callable[[AgentRole, str, str], Path],
    prompt_library: PromptLibrary,
) -> None:
    write_prompt(AgentRole.SECURITY, "v1", BODY)
    write_prompt(ROLE, "v1", BODY)

    assert prompt_library.roles() == [ROLE, AgentRole.SECURITY]
