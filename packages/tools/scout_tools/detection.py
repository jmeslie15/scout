"""Deterministic technology detection (spec section 15).

Spec section 15 asks the Repository Analyzer to identify languages, frameworks, package
managers, tests, databases, and so on. Most of that is *deterministic*: it can be read
from manifest files far more reliably than it can be inferred by a model. So the
deterministic part is implemented here, and the agent is left to do what models are
actually good at — explaining entry points, architecture boundaries, and intent.

This module never executes anything. It reads file names and a small amount of manifest
content, which makes it safe to run on an untrusted checkout and fast enough to run before
the sandbox is even started.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from scout_domain.models.repository import TechnologyStack
from scout_observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from scout_sandbox import Workspace

__all__ = [
    "DETECTION_MARKERS",
    "DetectedLayout",
    "detect_layout",
    "detect_technology_stack",
    "guess_service_roots",
]

_log = get_logger(__name__)

#: Marker file -> (languages, package managers, build tools). Presence of the file is the
#: signal; content parsing is used only where presence is ambiguous.
DETECTION_MARKERS: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]] = {
    "pyproject.toml": (("python",), ("pip",), ("poetry", "hatch", "setuptools")),
    "poetry.lock": (("python",), ("poetry",), ()),
    "uv.lock": (("python",), ("uv",), ()),
    "requirements.txt": (("python",), ("pip",), ()),
    "setup.py": (("python",), ("pip",), ("setuptools",)),
    "Pipfile": (("python",), ("pipenv",), ()),
    "package.json": (("typescript", "javascript"), ("npm", "pnpm", "yarn"), ("vite", "webpack")),
    "pnpm-lock.yaml": ((), ("pnpm",), ()),
    "yarn.lock": ((), ("yarn",), ()),
    "package-lock.json": ((), ("npm",), ()),
    "go.mod": (("go",), ("go",), ("go",)),
    "Cargo.toml": (("rust",), ("cargo",), ("cargo",)),
    "Gemfile": (("ruby",), ("bundler",), ()),
    "composer.json": (("php",), ("composer",), ()),
    "pom.xml": (("java",), ("maven",), ("maven",)),
    "build.gradle": (("java", "kotlin"), ("gradle",), ("gradle",)),
    "build.gradle.kts": (("kotlin",), ("gradle",), ("gradle",)),
    "*.csproj": (("csharp",), ("nuget",), ("msbuild",)),
    "Package.swift": (("swift",), ("swiftpm",), ("swift",)),
}

#: Framework and test-framework signals, detected by substring in manifest content.
#: Substring matching on a manifest is deliberately conservative: a false positive here
#: makes Scout run a tool that is not installed, which is caught by the tool layer's
#: applicability check anyway.
_PYTHON_FRAMEWORKS: Final[dict[str, str]] = {
    "fastapi": "fastapi",
    "django": "django",
    "flask": "flask",
    "starlette": "starlette",
    "aiohttp": "aiohttp",
    "pydantic": "pydantic",
    "sqlalchemy": "sqlalchemy",
    "celery": "celery",
}
_PYTHON_TEST_FRAMEWORKS: Final[dict[str, str]] = {
    "pytest": "pytest",
    "unittest": "unittest",
    "nose": "nose",
    "hypothesis": "hypothesis",
}
_JS_FRAMEWORKS: Final[dict[str, str]] = {
    '"react"': "react",
    '"next"': "next",
    '"vue"': "vue",
    '"svelte"': "svelte",
    '"express"': "express",
    '"nestjs"': "nestjs",
    '"@angular/core"': "angular",
    '"tailwindcss"': "tailwindcss",
}
_JS_TEST_FRAMEWORKS: Final[dict[str, str]] = {
    '"vitest"': "vitest",
    '"jest"': "jest",
    '"mocha"': "mocha",
    '"playwright"': "playwright",
    '"cypress"': "cypress",
}
_DATABASES: Final[dict[str, str]] = {
    "psycopg": "postgresql",
    "asyncpg": "postgresql",
    "postgres": "postgresql",
    "pg8000": "postgresql",
    "mysql": "mysql",
    "pymongo": "mongodb",
    "mongodb": "mongodb",
    "redis": "redis",
    "sqlite": "sqlite",
    "sqlalchemy": "sqlalchemy",
    "prisma": "prisma",
}
_CI_MARKERS: Final[dict[str, str]] = {
    ".github/workflows": "github-actions",
    ".gitlab-ci.yml": "gitlab-ci",
    "Jenkinsfile": "jenkins",
    ".circleci": "circleci",
    "azure-pipelines.yml": "azure-pipelines",
    "buildkite.yml": "buildkite",
}
_LANGUAGE_BY_EXTENSION: Final[dict[str, str]] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".cs": "csharp",
    ".php": "php",
    ".scala": "scala",
    ".sh": "shell",
    ".sql": "sql",
    ".tf": "hcl",
    ".ex": "elixir",
    ".exs": "elixir",
}


@dataclass(frozen=True, slots=True)
class DetectedLayout:
    """Where the interesting things live inside a repository."""

    root_files: frozenset[str]
    all_files: tuple[str, ...]
    service_roots: tuple[str, ...]
    test_roots: tuple[str, ...]
    documentation_roots: tuple[str, ...]
    infra_roots: tuple[str, ...]
    migration_roots: tuple[str, ...]
    monorepo: bool


def _matches_marker(filename: str, marker: str) -> bool:
    if marker.startswith("*"):
        return filename.endswith(marker[1:])
    return filename == marker


def detect_layout(workspace: Workspace, *, limit: int = 50_000) -> DetectedLayout:
    """Inspect the checkout to find where source, tests, docs, and infra live.

    Reads file *names* only. Scout does not read every file in an untrusted repository to
    decide where to look; it looks at structure first and content second, on demand.
    """
    files = workspace.list_files(limit=limit)
    root_files = frozenset(entry.split("/", 1)[0] for entry in files if "/" not in entry)

    service_roots: list[str] = []
    test_roots: list[str] = []
    documentation_roots: list[str] = []
    infra_roots: list[str] = []
    migration_roots: list[str] = []

    top_level_dirs = sorted({entry.split("/", 1)[0] for entry in files if "/" in entry})

    for directory in top_level_dirs:
        lowered = directory.lower()
        if lowered in {"test", "tests", "spec", "specs", "__tests__", "e2e"}:
            test_roots.append(directory)
        if lowered in {"docs", "doc", "documentation"}:
            documentation_roots.append(directory)
        if lowered in {"migrations", "alembic", "db", "database", "flyway", "liquibase"}:
            migration_roots.append(directory)
        if lowered in {
            "infra",
            "infrastructure",
            "terraform",
            "k8s",
            "kubernetes",
            "helm",
            "deploy",
            "deployment",
            "charts",
            "ops",
            ".github",
        }:
            infra_roots.append(directory)

    # Services: a first- or second-level directory containing a manifest is a deployable
    # unit. This is the heuristic that finds `apps/api`, `services/billing`, `packages/foo`.
    manifest_names = {
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "Dockerfile",
        "pom.xml",
        "build.gradle",
    }
    for entry in files:
        parts = entry.split("/")
        if len(parts) < 2:
            continue
        if parts[-1] not in manifest_names:
            continue
        depth = len(parts) - 1
        if depth <= 2:
            service_roots.append("/".join(parts[:-1]))

    monorepo = bool({"apps", "packages", "services", "libs"} & set(top_level_dirs))

    return DetectedLayout(
        root_files=root_files,
        all_files=tuple(files),
        service_roots=tuple(sorted(set(filter(None, service_roots)))),
        test_roots=tuple(sorted(set(test_roots))),
        documentation_roots=tuple(sorted(set(documentation_roots))),
        infra_roots=tuple(sorted(set(infra_roots))),
        migration_roots=tuple(sorted(set(migration_roots))),
        monorepo=monorepo,
    )


def detect_technology_stack(
    workspace: Workspace, *, layout: DetectedLayout | None = None
) -> TechnologyStack:
    """Build a :class:`TechnologyStack` from the checkout (spec section 15)."""
    resolved_layout = layout or detect_layout(workspace)

    languages: dict[str, None] = {}
    package_managers: dict[str, None] = {}
    build_tools: dict[str, None] = {}
    frameworks: dict[str, None] = {}
    test_frameworks: dict[str, None] = {}
    databases: dict[str, None] = {}
    ci_systems: dict[str, None] = {}
    containerized = False
    has_iac = False

    # 1. Manifest markers, at the repository root or inside a service directory.
    basenames = {entry.rsplit("/", 1)[-1] for entry in resolved_layout.all_files}
    for marker, (langs, managers, builders) in DETECTION_MARKERS.items():
        if not any(_matches_marker(name, marker) for name in basenames):
            continue
        for language in langs:
            languages.setdefault(language, None)
        for manager in managers:
            package_managers.setdefault(manager, None)
        for builder in builders:
            build_tools.setdefault(builder, None)

    # 2. Extension census, so a repository with no manifest still gets a language.
    for entry in resolved_layout.all_files:
        suffix = Path(entry).suffix.lower()
        detected_language = _LANGUAGE_BY_EXTENSION.get(suffix)
        if detected_language is not None:
            languages.setdefault(detected_language, None)

    # 3. Manifest content, for frameworks and test runners.
    _scan_manifest(
        workspace,
        candidates=("pyproject.toml", "requirements.txt", "setup.py", "Pipfile"),
        frameworks=_PYTHON_FRAMEWORKS,
        test_frameworks=_PYTHON_TEST_FRAMEWORKS,
        databases=_DATABASES,
        frameworks_out=frameworks,
        tests_out=test_frameworks,
        databases_out=databases,
    )
    _scan_manifest(
        workspace,
        candidates=("package.json",),
        frameworks=_JS_FRAMEWORKS,
        test_frameworks=_JS_TEST_FRAMEWORKS,
        databases=_DATABASES,
        frameworks_out=frameworks,
        tests_out=test_frameworks,
        databases_out=databases,
    )

    # 4. A test root without a declared framework is almost always pytest for Python.
    if resolved_layout.test_roots and "pytest" not in test_frameworks:
        if "python" in languages:
            test_frameworks.setdefault("pytest", None)

    # 5. CI, containers, infrastructure.
    for marker, system in _CI_MARKERS.items():
        if marker in resolved_layout.root_files or any(
            entry.startswith(marker + "/") for entry in resolved_layout.all_files
        ):
            ci_systems.setdefault(system, None)

    containerized = (
        any(
            name == "Dockerfile" or name.startswith("Dockerfile.")
            for name in resolved_layout.root_files
        )
        or any(
            entry.endswith(("/Dockerfile", ".dockerfile")) for entry in resolved_layout.all_files
        )
        or "docker-compose.yml" in resolved_layout.root_files
        or "compose.yaml" in resolved_layout.root_files
    )

    # Infrastructure-as-code is a distinct risk signal (see scout_domain.risk), so it is
    # detected separately from "there is an infra directory".
    has_iac = any(
        Path(entry).suffix.lower() in {".tf", ".tfvars"}
        or Path(entry).name in {"serverless.yml", "Pulumi.yaml", "cloudformation.yaml"}
        for entry in resolved_layout.all_files
    ) or any(root in {"terraform", "pulumi"} for root in resolved_layout.infra_roots)

    return TechnologyStack(
        languages=sorted(languages),
        frameworks=sorted(frameworks),
        package_managers=sorted(package_managers),
        test_frameworks=sorted(test_frameworks),
        databases=sorted(databases),
        build_tools=sorted(build_tools),
        ci_systems=sorted(ci_systems),
        containerized=containerized,
        has_infrastructure_as_code=has_iac,
    )


def _scan_manifest(
    workspace: Workspace,
    *,
    candidates: Iterable[str],
    frameworks: dict[str, str],
    test_frameworks: dict[str, str],
    databases: dict[str, str],
    frameworks_out: dict[str, None],
    tests_out: dict[str, None],
    databases_out: dict[str, None],
) -> None:
    """Read the manifests that exist and merge in any signals found."""
    for candidate in candidates:
        if not workspace.exists(candidate):
            continue
        try:
            content = workspace.read_text(candidate, max_bytes=512_000).lower()
        except Exception:
            _log.debug("could not read manifest", path=candidate)
            continue

        # package.json is JSON; its dependency names are quoted, which is why the JS
        # tables above include the quotes. Parsing it properly avoids matching a
        # description string that happens to contain the word "react".
        if candidate == "package.json":
            content = _normalise_package_json(content)

        for needle, name in {**frameworks, **test_frameworks, **databases}.items():
            if needle in content:
                if needle in frameworks:
                    frameworks_out.setdefault(name, None)
                if needle in test_frameworks:
                    tests_out.setdefault(name, None)
                if needle in databases:
                    databases_out.setdefault(name, None)


def _normalise_package_json(content: str) -> str:
    """Extract dependency names from a ``package.json`` body.

    Returns a quoted, comma-joined string so the same substring tables work for Python and
    JavaScript manifests. Invalid JSON falls back to the raw text, because a broken
    ``package.json`` should still yield *some* signal rather than none.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return content
    if not isinstance(parsed, dict):
        return content
    names: list[str] = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = parsed.get(key)
        if isinstance(section, dict):
            names.extend(str(name) for name in section)
    return " ".join(f'"{name.lower()}"' for name in names)


def guess_service_roots(layout: DetectedLayout) -> list[str]:
    """Return candidate service roots, preferring monorepo conventions.

    Used to attribute a changed file to a service so that verification can be scoped; a
    change to ``apps/api`` should not trigger the ``apps/dashboard`` test suite.
    """
    if not layout.monorepo:
        return ["."]
    preferred = [
        root
        for root in layout.service_roots
        if root.split("/", 1)[0] in {"apps", "packages", "services", "libs"}
    ]
    return preferred or list(layout.service_roots) or ["."]


_SOURCE_SUFFIXES: Final[frozenset[str]] = frozenset(_LANGUAGE_BY_EXTENSION)
_DOC_SUFFIXES: Final[frozenset[str]] = frozenset({".md", ".rst", ".txt", ".adoc"})
_TEST_HINT = re.compile(r"(^|/)(tests?|spec|specs|__tests__)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$")


def classify_paths(paths: Iterable[str]) -> dict[str, list[str]]:
    """Group paths into coarse buckets used by risk classification and context retrieval."""
    buckets: dict[str, list[str]] = {
        "source": [],
        "tests": [],
        "docs": [],
        "infra": [],
        "migrations": [],
        "other": [],
    }
    for path in paths:
        lowered = path.lower()
        suffix = Path(path).suffix.lower()
        if _TEST_HINT.search(lowered):
            buckets["tests"].append(path)
        elif suffix in _DOC_SUFFIXES:
            buckets["docs"].append(path)
        elif any(
            token in lowered for token in ("migrations/", "alembic/", "flyway/", "liquibase/")
        ):
            buckets["migrations"].append(path)
        elif (
            any(
                token in lowered
                for token in ("infra/", "terraform/", "k8s/", "helm/", "dockerfile", "deploy/")
            )
            or suffix == ".tf"
        ):
            buckets["infra"].append(path)
        elif suffix in _SOURCE_SUFFIXES:
            buckets["source"].append(path)
        else:
            buckets["other"].append(path)
    return buckets
