# syntax=docker/dockerfile:1
# =============================================================================
# Scout application image — SHARED BUILD DEFINITION.
# Application: worker — Disposable execution plane: checkout, sandboxed tools, agent calls.
#
# The three control-plane applications (`apps/api`, `apps/orchestrator`,
# `apps/worker`) are members of one uv workspace and differ only in the
# directory/package they run. Docker has no `include`/`extends` mechanism for
# Dockerfiles, so the body below is repeated verbatim in api.Dockerfile,
# orchestrator.Dockerfile and worker.Dockerfile. The ONLY intentional
# differences between the three files are the two ARG defaults and the single
# sentence naming the application. Keep it that way, and apply any change to all
# three files in the same commit.
#
# Build context: the repository root, e.g.
#   docker build -f infra/docker/api.Dockerfile -t scout-api:local .
# =============================================================================
ARG PYTHON_VERSION=3.12
# Pinned so image rebuilds are reproducible; bump deliberately.
ARG UV_VERSION=0.5.11

# --- Builder: resolve the locked workspace into a self-contained virtualenv --
FROM python:${PYTHON_VERSION}-slim AS builder

ARG UV_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Hardlinks across Docker layers are unreliable; copy instead.
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # uv creates the venv next to the workspace so it can be copied as a unit.
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app

# Manifests and lockfile first: this layer is cached until dependencies change.
COPY pyproject.toml ./
COPY uv.lock ./
COPY apps ./apps
COPY packages ./packages

# `uv sync --frozen --no-dev` installs exactly the locked dependency set without
# the dev toolchain (pytest/ruff/mypy stay out of runtime images).
#
# `--all-packages` is required, not optional: the workspace root
# (`package = false`) depends on `packages/*` only, so a plain root sync would
# leave the applications' own runtime dependencies (fastapi, uvicorn, redis, ...)
# out of the image entirely. This image serves all three applications, so every
# member is installed.
#
# Fallbacks, in order: a stale/absent uv.lock cannot be used with `--frozen`, so
# drop to `--locked`, then to a plain sync, warning loudly each time instead of
# failing the build with an opaque resolver error.
RUN set -eux; \
    if [ -f uv.lock ]; then \
        uv sync --all-packages --frozen --no-dev --no-editable \
        || { echo "WARNING: uv.lock out of date; retrying with --locked" >&2; \
             uv sync --all-packages --locked --no-dev --no-editable; } \
        || { echo "WARNING: uv.lock unusable; retrying with a plain sync" >&2; \
             uv sync --all-packages --no-dev --no-editable; }; \
    else \
        echo "WARNING: uv.lock not found; falling back to a non-frozen sync" >&2; \
        uv sync --all-packages --no-dev --no-editable; \
    fi

# Fail the build here rather than at runtime if the applications' runtime
# dependencies are missing. The workspace interpreter is used explicitly because
# the builder PATH does not point at the venv.
RUN /app/.venv/bin/python -c "import fastapi, uvicorn"

# --- Runtime -----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

# Defaults are overridden per application by the wrapper files.
ARG APP_PATH=apps/worker
ARG APP_PACKAGE=scout_worker
ARG SCOUT_UID=1000
ARG SCOUT_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Put the workspace venv on PATH so `uvicorn`, `alembic` and
    # `python -m scout_*` resolve without an explicit `uv run`.
    PATH="/app/.venv/bin:${PATH}" \
    VIRTUAL_ENV=/app/.venv \
    # The workspace root (`uv sync` with `package = false`) installs the
    # `packages/*` libraries but NOT the three apps — they are not root
    # dependencies. This project has no `[project.scripts]` entry points and
    # runs apps as source trees (`python -m scout_orchestrator`,
    # `uvicorn scout_api.main:app`), so the app source roots must be importable.
    PYTHONPATH="/app/apps/api:/app/apps/orchestrator:/app/apps/worker:/app" \
    # Runtime configuration always comes from the environment (compose/.env).
    # NEVER bake values here — this image is shared by every environment.
    SCOUT_ENV=development

# Non-root identity plus the runtime state directories. The application writes
# artifacts and workspaces under /app/var (SCOUT_ARTIFACT_ROOT /
# SCOUT_WORKSPACE_ROOT); they must exist and be owned by the app user, because
# the container never runs as root and /app itself stays root-owned.
RUN set -eux; \
    groupadd --gid "${SCOUT_GID}" scout; \
    useradd --uid "${SCOUT_UID}" --gid "${SCOUT_GID}" --create-home --shell /usr/sbin/nologin scout; \
    mkdir -p /app/var/artifacts /app/var/workspaces; \
    chown -R scout:scout /app/var

WORKDIR /app

# Locked virtualenv from the builder stage.
COPY --from=builder /app/.venv /app/.venv

# Workspace sources, owned by the non-root user so `--reload` and bind-mounted
# edits behave normally.
COPY --chown=scout:scout . /app

USER scout

# Generic liveness check: a broken interpreter or venv fails immediately.
# The API overrides this with an HTTP check in docker-compose.yml.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys; sys.exit(0)"]

# No implicit long-running command: compose (or an operator) supplies it.
CMD ["python", "-c", "print('scout app image: pass a command, e.g. python -m scout_orchestrator')"]
