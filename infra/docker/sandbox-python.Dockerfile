# syntax=docker/dockerfile:1
# =============================================================================
# Scout sandbox image — RUNS UNTRUSTED REPOSITORY CODE.
#
# !! SECURITY CONTRACT — READ BEFORE EDITING !!
#
# 1. This image executes arbitrary, untrusted code from third-party
#    repositories. Treat everything inside it as hostile.
# 2. This image MUST NEVER contain control-plane secrets: no GitHub App
#    private keys, no webhook secrets, no LLM API keys, no database
#    credentials, no `.env` files, no cloud credentials. The orchestrator
#    passes only the repository checkout; anything else is a vulnerability.
# 3. It is invoked with `--network none` by default (see
#    SCOUT_SANDBOX_NETWORK in .env.example and scripts/sandbox_smoke.sh).
#    Do not add network tooling or a listening service.
# 4. It is invoked with `--read-only --tmpfs /tmp`, a memory cap, a CPU cap
#    and a PID limit. Do not add anything that needs a writable root
#    filesystem or privileges.
# 5. Keep the toolchain minimal: git + Python 3.12 + Node 20 exist only
#    because repositories under verification need them for tests. Every
#    additional package increases the attack surface for no benefit.
# 6. Never run this image as root, and never mount the Docker socket,
#    host paths outside the checkout, or any Docker secret into it.
# =============================================================================
FROM python:3.12-slim

# Toolchain needed to *run* repository tests. No control-plane packages.
# hadolint ignore=DL3008,DL3015
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        make \
        xz-utils; \
    # Node.js 20 LTS for JavaScript/TypeScript repositories.
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    npm cache clean --force; \
    apt-get purge -y --auto-remove gnupg; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /root/.npm

# Non-root identity used for every untrusted command.
RUN set -eux; \
    groupadd --gid 1000 scout; \
    useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin scout

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    HOME=/home/scout

# Repository checkout is mounted here; no image files live here.
RUN install -d -o scout -g scout -m 0755 /workspace
WORKDIR /workspace

USER scout

# The sandbox is short-lived per command, so this only proves the image
# itself is functional (toolchain present, non-root, workspace writable).
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD git --version >/dev/null 2>&1 \
        && python -c "import sys" \
        && node --version >/dev/null 2>&1 \
        && test -d /workspace \
        || exit 1

# No CMD on purpose: the orchestrator always supplies the exact command.
