"""Fixtures the vertical slice needs: a real git repository and a fake GitHub.

The slice is only meaningful if the repository is real. A mocked checkout would prove that Scout
calls a function, not that Scout can fetch a pinned commit, run the repository's own suite in a
sandbox, and report what it observed. So the fixture builds an actual repository with two
commits, an actual bare remote that accepts a fetch by SHA, and a trivial Python package whose
tests either pass or fail.

The GitHub side is the shipped :class:`~scout_github.FakeGitHubClient`, which is what makes the
slice runnable offline. GitHub's role here is small but load-bearing: it is where the pull
request *body* comes from, and the body is the requirement material FEATURE_VERIFYING works from.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scout_github import FakeGitHubClient

__all__ = ["GitFixture", "build_git_fixture", "register_pull_request"]

#: The repository identity the fixture uses. ``owner/name``, because that is what the clone URL is
#: built from.
FULL_NAME = "acme/widgets"
PULL_REQUEST_NUMBER = 7

#: The requirement text the fixture's pull request states. FEATURE_VERIFYING extracts its
#: acceptance criteria from this, and the passing test is named so a criterion overlaps it.
PR_BODY = "Adds a multiply() helper to widget.py so callers can scale values.\n"

_HAPPY_TEST = (
    "from widget import add, multiply\n\n\n"
    "def test_add():\n"
    "    assert add(1, 2) == 3\n\n\n"
    "def test_multiply():\n"
    "    assert multiply(2, 3) == 6\n"
)

_BROKEN_TEST = "from widget import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"

_WIDGET_BASE = "def add(a, b):\n    return a + b\n"

_WIDGET_HAPPY = "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n"

#: A deliberately wrong implementation: ``add`` subtracts. The suite that ships with the
#: repository still asserts the correct behaviour, so the run genuinely fails rather than erroring.
_WIDGET_BROKEN = "def add(a, b):\n    return a - b\n"

_PYPROJECT = """\
[project]
name = "widget"
version = "0.1.0"
requires-python = ">=3.12"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""

#: Repository configuration for the slice-1 fixture (spec section 83).
#:
#: Two checks are switched off, and both are recorded as skips with this file as the reason:
#:
#: * **Security scanning**, because no scanner is installed on a typical developer machine and a
#:   check that cannot run is (correctly) reported as unverified.
#: * **Feature verification**, because it is not part of the first vertical slice. Section 83 covers
#:   PR -> webhook -> job -> checkout -> tests -> results -> report -> check; criteria verification
#:   is section 85's slice, and the feature-verification agent is not wired yet. Leaving it on would
#:   make every stated criterion ``not_verified`` and therefore every clean change
#:   ``REQUIRES_HUMAN_REVIEW`` -- correct behaviour, but not this slice's subject. The third test in
#:   the file re-enables it precisely to prove that gate still holds.
_ENGINEERING_YAML = """\
project:
  name: widget

verification:
  disabled_stages:
    - SECURITY_SCANNING

feature_verification:
  enabled: false
"""

_ENGINEERING_YAML_VERIFYING = """\
project:
  name: widget

verification:
  disabled_stages:
    - SECURITY_SCANNING

feature_verification:
  enabled: true
"""


@dataclass(frozen=True, slots=True)
class GitFixture:
    """A real repository with two commits and a fetchable remote."""

    root: Path
    remote_url: str
    base_sha: str
    head_sha: str
    full_name: str = FULL_NAME
    pull_request_number: int = PULL_REQUEST_NUMBER
    pr_body: str = PR_BODY


def build_git_fixture(
    root: Path,
    *,
    failing: bool,
    feature_verification: bool = False,
    full_name: str = FULL_NAME,
    pull_request_number: int = PULL_REQUEST_NUMBER,
) -> GitFixture:
    """Create the origin, the bare remote, and the two commits.

    Args:
        root: A temporary directory the caller owns.
        failing: When true, the head commit breaks ``add`` without touching the test, so the
            repository's own suite fails and the change is not covered by any test modification.
        feature_verification: When true, ``feature_verification.enabled`` is left on, so the
            acceptance criteria extracted from the pull-request body are assessed and reported as
            unverified. When false the stage is skipped with a recorded reason.
        full_name: Repository identity, as ``owner/name``.
        pull_request_number: The pull request the job is about.

    Returns:
        A :class:`GitFixture` naming the real SHAs and the ``file://`` remote URL.

    The bare remote needs ``uploadpack.allowAnySHA1InWant``: Scout fetches a *specific* SHA rather
    than a branch (spec section 13 forbids analysing an unpinned ref), and git refuses an arbitrary
    SHA fetch unless the server allows it.
    """
    origin = root / "origin"
    origin.mkdir(parents=True)

    _write(origin / "pyproject.toml", _PYPROJECT)
    _write(origin / "conftest.py", "")
    _write(
        origin / ".scout" / "engineering.yaml",
        _ENGINEERING_YAML_VERIFYING if feature_verification else _ENGINEERING_YAML,
    )
    _write(origin / "widget.py", _WIDGET_BASE)
    _write(origin / "tests" / "test_widget.py", _BROKEN_TEST)

    _git(origin, "init", "-q", "--initial-branch=main")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "Add the widget module")
    base_sha = _git(origin, "rev-parse", "HEAD")

    if failing:
        _write(origin / "widget.py", _WIDGET_BROKEN)
        message = "Change how add() combines its arguments"
    else:
        _write(origin / "widget.py", _WIDGET_HAPPY)
        _write(origin / "tests" / "test_widget.py", _HAPPY_TEST)
        message = "Add multiply() helper"
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", message)
    head_sha = _git(origin, "rev-parse", "HEAD")

    remote = root / "remotes" / f"{full_name}.git"
    remote.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ("git", "clone", "-q", "--bare", str(origin), str(remote)), check=True, env=_git_env()
    )
    _git(remote, "config", "uploadpack.allowAnySHA1InWant", "true")
    _git(remote, "config", "uploadpack.allowReachableSHA1InWant", "true")

    return GitFixture(
        root=root,
        remote_url=f"file://{remote}",
        base_sha=base_sha,
        head_sha=head_sha,
        full_name=full_name,
        pull_request_number=pull_request_number,
    )


def register_pull_request(
    github: FakeGitHubClient,
    fixture: GitFixture,
    *,
    title: str = "Add a multiply() helper",
    body: str | None = None,
) -> None:
    """Register the pull request the job is about, pinned to the fixture's real SHAs.

    The SHAs are the ones git produced, not placeholders: a fixture whose GitHub view disagreed with
    its checkout would let a SHA mismatch pass unnoticed.
    """
    github.register_repository(fixture.full_name)
    github.register_pull_request(
        fixture.full_name,
        fixture.pull_request_number,
        title=title,
        body=fixture.pr_body if body is None else body,
        base_sha=fixture.base_sha,
        head_sha=fixture.head_sha,
    )


def _write(path: Path, content: str) -> None:
    """Write ``content`` to ``path``, creating parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git_env() -> dict[str, str]:
    """Return an environment with a deterministic commit identity.

    Without it, git refuses to commit on a machine with no ``user.email`` configured, which would
    make the slice fail for a reason that has nothing to do with Scout.
    """
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Scout Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@scout.test",
        "GIT_COMMITTER_NAME": "Scout Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@scout.test",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(cwd: Path, *args: str) -> str:
    """Run git and return its stripped stdout."""
    result = subprocess.run(
        ("git", *args),
        cwd=cwd,
        env=_git_env(),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
