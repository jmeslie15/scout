"""Template loading, sandboxing, autoescaping, and strict-undefined behaviour.

Templates render repository-derived and model-derived text, so they are treated as an
untrusted-input boundary. These tests pin the two properties that make that safe —
autoescaping of HTML and strict undefined — and the loading property that makes the
package installable (templates come from the package, not from a working directory).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jinja2 import DictLoader, TemplateNotFound, UndefinedError
from jinja2.exceptions import SecurityError

from scout_notifications.templating import TemplateRenderer, build_environment

EXPECTED_TEMPLATES = {
    "approval_required.html.j2",
    "approval_required.md.j2",
    "critical_failure.html.j2",
    "critical_failure.md.j2",
    "question.html.j2",
    "question.md.j2",
    "verification_complete.html.j2",
    "verification_complete.md.j2",
}


def _question_context(question, job, job_url: str, *, title: str | None = None) -> dict[str, Any]:
    """A realistic question context, mirroring what the service supplies."""
    if title is not None:
        question = question.evolve(title=title)
    return {
        "question": question,
        "job": job,
        "job_url": job_url,
        "repository_full_name": job.repository_full_name,
        "pull_request_number": job.pull_request_number,
        "severity": question.severity.value,
        "why_asking": question.context or question.description,
        "investigation_summary": question.investigation_summary,
        "evidence": list(question.evidence),
        "options": list(question.options),
        "default_assumption": question.default_assumption,
    }


@pytest.fixture
def renderer() -> TemplateRenderer:
    """The default renderer, loading templates from the installed package."""
    return TemplateRenderer()


def test_every_packaged_template_is_loadable(renderer: TemplateRenderer) -> None:
    assert renderer.available_templates() == EXPECTED_TEMPLATES


def test_templates_load_from_package_resources_not_a_directory(renderer: TemplateRenderer) -> None:
    """The loader must be resource-based so rendering works from an installed wheel."""
    assert isinstance(renderer.environment.loader, DictLoader)


def test_question_markdown_renders_the_required_elements(
    renderer: TemplateRenderer,
    make_question,
    make_job,
) -> None:
    job = make_job()
    question = make_question()
    context = _question_context(question, job, "https://scout.test/jobs/job_1")

    rendered = renderer.render("question.md.j2", context)

    assert question.title in rendered
    assert question.description in rendered
    assert question.context in rendered
    assert "app/api/export.py" in rendered
    assert "https://scout.test/jobs/job_1" in rendered
    assert "Managers may see metadata" in rendered
    assert "Only owners may" in rendered


def test_html_templates_autoescape_untrusted_text(
    renderer: TemplateRenderer,
    make_question,
    make_job,
) -> None:
    """A repository-authored title must not become executable markup in an email."""
    injected = "<script>alert('pwned')</script>"
    job = make_job()
    question = make_question()
    context = _question_context(question, job, "https://scout.test/jobs/job_1", title=injected)

    rendered = renderer.render("question.html.j2", context)

    assert "<script>alert" not in rendered
    assert "&lt;script&gt;" in rendered


def test_markdown_templates_do_not_escape(
    renderer: TemplateRenderer,
    make_question,
    make_job,
) -> None:
    """Autoescaping Markdown would corrupt it, so the .md templates stay literal."""
    injected = "<script>alert('pwned')</script>"
    job = make_job()
    question = make_question()
    context = _question_context(question, job, "https://scout.test/jobs/job_1", title=injected)

    rendered = renderer.render("question.md.j2", context)

    assert injected in rendered


def test_strict_undefined_raises_instead_of_rendering_blank(
    renderer: TemplateRenderer,
) -> None:
    with pytest.raises(UndefinedError):
        renderer.render("question.md.j2", {})


def test_unknown_template_raises(renderer: TemplateRenderer) -> None:
    with pytest.raises(TemplateNotFound):
        renderer.render("does_not_exist.md.j2", {})


def test_environment_can_load_from_an_explicit_directory(tmp_path: Path) -> None:
    (tmp_path / "greeting.md.j2").write_text("Hello {{ name }}!", encoding="utf-8")
    environment = build_environment(tmp_path)
    renderer = TemplateRenderer(environment=environment)

    rendered = renderer.render("greeting.md.j2", {"name": "<world>"})

    assert rendered == "Hello <world>!"


def test_custom_html_suffix_is_autoescaped_in_an_explicit_directory(tmp_path: Path) -> None:
    (tmp_path / "greeting.html.j2").write_text("<p>{{ name }}</p>", encoding="utf-8")
    renderer = TemplateRenderer(environment=build_environment(tmp_path))

    rendered = renderer.render("greeting.html.j2", {"name": "<world>"})

    assert rendered == "<p>&lt;world&gt;</p>"


def test_sandbox_blocks_underscore_attribute_access(
    renderer: TemplateRenderer,
) -> None:
    """The sandbox stops a template from walking an object graph to reach internals."""
    environment = renderer.environment
    environment.loader = DictLoader({"probe.md.j2": "{{ question.__class__ }}"})  # type: ignore[assignment]
    with pytest.raises(SecurityError, match=r"(?i)unsafe"):
        environment.get_template("probe.md.j2").render(question=object())
