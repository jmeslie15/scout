"""Jinja2 rendering for notification bodies.

Three deliberate choices:

**Sandboxed environment.** Notification templates render repository-derived text
(PR titles, file paths, commit messages) and model-derived text (findings, summaries).
Both are untrusted input as far as this package is concerned, so the environment is
``jinja2.sandbox.SandboxedEnvironment``: it blocks attribute access to names beginning
with an underscore and refuses to call unsafe callables. A template cannot walk an
object graph to reach ``__globals__``, and a malicious repository cannot use a template
variable to execute code in the API process.

**Autoescape for HTML only.** Autoescaping Markdown would corrupt it (``*`` becomes
``&#42;``) and autoescaping nothing would make the HTML email part an injection vector.
``select_autoescape`` is therefore keyed on the ``.html.j2`` suffix, with strings rendered
directly (``default_for_string=False``) escaping nothing — callers that build HTML must do
it through a ``.html.j2`` template, not through ``from_string``.

**Strict undefined.** ``StrictUndefined`` raises instead of rendering an empty string.
A notification that silently loses the "why Scout is asking" section is worse than one
that fails loudly in the worker log, because the human sees a confident but incomplete
request and cannot tell the difference.

**Template location.** The templates live at ``scout_notifications/templates/`` — inside
the import package — because ``pyproject.toml`` selects ``packages = ["scout_notifications"]``
for the wheel and package data travels with the package directory. A sibling
``packages/notifications/templates/`` directory would not be shipped, and the
:mod:`importlib.resources` lookup below would fail from an installed wheel.
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from jinja2 import BaseLoader, DictLoader, Environment, FileSystemLoader, StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

__all__ = ["TemplateRenderer", "build_environment"]

#: Import package that owns the bundled templates.
_PACKAGE: Final[str] = "scout_notifications"
_TEMPLATES_DIRNAME: Final[str] = "templates"
_TEMPLATE_SUFFIX: Final[str] = ".j2"

#: Suffixes whose rendered output is HTML and must be autoescaped. ``select_autoescape``
#: matches on the end of the template name, and our HTML templates end in ``.html.j2``.
_HTML_EXTENSIONS: Final[tuple[str, ...]] = ("html.j2", "html")


def build_environment(template_dir: Path | None = None) -> Environment:
    """Build the sandboxed environment, optionally loading templates from a directory.

    When ``template_dir`` is ``None`` the templates are read from the installed package via
    :mod:`importlib.resources`, so rendering works from a wheel and not only from a source
    checkout.
    """
    return SandboxedEnvironment(
        loader=_build_loader(template_dir),
        undefined=StrictUndefined,
        autoescape=_select_autoescape,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _select_autoescape(template_name: str | None) -> bool:
    """Autoescape HTML templates only; leave Markdown untouched."""
    if template_name is None:
        return False
    return template_name.endswith(tuple(f".{extension}" for extension in _HTML_EXTENSIONS))


def _build_loader(template_dir: Path | None) -> BaseLoader:
    """Return a loader for ``template_dir`` or for the packaged templates."""
    if template_dir is not None:
        return FileSystemLoader(str(template_dir))

    package_templates = importlib.resources.files(_PACKAGE).joinpath(_TEMPLATES_DIRNAME)
    contents = {
        entry.name: entry.read_text(encoding="utf-8")
        for entry in package_templates.iterdir()
        if entry.name.endswith(_TEMPLATE_SUFFIX)
    }
    return DictLoader(contents)


class TemplateRenderer:
    """Render notification templates by name."""

    def __init__(
        self,
        *,
        template_dir: Path | None = None,
        environment: Environment | None = None,
    ) -> None:
        self._environment = (
            environment if environment is not None else build_environment(template_dir)
        )

    @property
    def environment(self) -> Environment:
        """The underlying environment, for callers that need to add filters."""
        return self._environment

    def render(self, template_name: str, context: Mapping[str, Any]) -> str:
        """Render ``template_name`` with ``context``.

        Raises:
            jinja2.TemplateNotFound: if the template does not exist.
            jinja2.UndefinedError: if the template references a missing variable. This is
                the strict-undefined contract: a blank section is never acceptable.
        """
        template = self._environment.get_template(template_name)
        return template.render(**context)

    def available_templates(self) -> set[str]:
        """Return the names of every loadable template."""
        return set(self._environment.list_templates())
