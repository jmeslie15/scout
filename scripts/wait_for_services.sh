#!/usr/bin/env bash
# =============================================================================
# wait_for_services.sh — block until the Scout datastores accept connections.
#
# Polls PostgreSQL and Redis *inside their containers* so it needs no host-side
# client tooling (pg_isready / redis-cli stay in the images). Exits non-zero
# with an actionable message if either service does not become ready in time.
#
# Usage:
#   scripts/wait_for_services.sh [timeout_seconds]
#
# Environment:
#   COMPOSE_FILE  compose file to use (default: infra/compose/docker-compose.yml)
#   PG_SERVICE    postgres service name   (default: postgres)
#   REDIS_SERVICE redis service name      (default: redis)
#   TIMEOUT       total seconds to wait   (default: 60; arg 1 wins)
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/infra/compose/docker-compose.yml}"
PG_SERVICE="${PG_SERVICE:-postgres}"
REDIS_SERVICE="${REDIS_SERVICE:-redis}"
TIMEOUT="${1:-${TIMEOUT:-60}}"
POLL_INTERVAL="${POLL_INTERVAL:-2}"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
    echo "ERROR: compose file not found: ${COMPOSE_FILE}" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not on PATH." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: cannot talk to the Docker daemon. Is Docker running?" >&2
    exit 1
fi

compose() {
    docker compose -f "${COMPOSE_FILE}" "$@"
}

# Wait until a container of the given service is running.
wait_for_container() {
    local service="$1"
    local deadline=$((SECONDS + TIMEOUT))
    local cid=""

    while ((SECONDS < deadline)); do
        cid="$(compose ps -q "${service}" 2>/dev/null || true)"
        if [[ -n "${cid}" ]]; then
            local running
            running="$(docker inspect --format '{{.State.Running}}' "${cid}" 2>/dev/null || echo false)"
            if [[ "${running}" == "true" ]]; then
                return 0
            fi
        fi
        sleep "${POLL_INTERVAL}"
    done

    echo "ERROR: ${service} did not start within ${TIMEOUT}s." >&2
    echo "       Start the datastores first: make up" >&2
    compose ps "${service}" >&2 || true
    return 1
}

# Wait until an exec'd probe command succeeds (checked from inside the image).
#
# Contract: $1 = compose service, $2.. = the probe command. There is deliberately
# no separate "description" argument — it is derived from the command, so a call
# cannot silently shift the command words into a description slot.
wait_for_probe() {
    local service="$1"
    shift 1
    local -a probe=("$@")
    local description
    description="$(printf '%s ' "${probe[@]}")"
    local deadline=$((SECONDS + TIMEOUT))

    if [ "${#probe[@]}" -eq 0 ]; then
        echo "ERROR: no probe command given for service '${service}'." >&2
        return 1
    fi

    while ((SECONDS < deadline)); do
        if compose exec -T "${service}" "${probe[@]}" >/dev/null 2>&1; then
            return 0
        fi
        sleep "${POLL_INTERVAL}"
    done

    echo "ERROR: probe '${description}' against ${service} did not become ready within ${TIMEOUT}s." >&2
    echo "       Inspect the logs with: make logs" >&2
    return 1
}

echo "Waiting up to ${TIMEOUT}s for postgres and redis (${COMPOSE_FILE})..."

wait_for_container "${PG_SERVICE}"
echo "  - ${PG_SERVICE}: container running"

wait_for_container "${REDIS_SERVICE}"
echo "  - ${REDIS_SERVICE}: container running"

wait_for_probe "${PG_SERVICE}" \
    pg_isready -U "${POSTGRES_USER:-scout}" -d "${POSTGRES_DB:-scout}"
echo "  - ${PG_SERVICE}: accepting connections"

wait_for_probe "${REDIS_SERVICE}" redis-cli ping
echo "  - ${REDIS_SERVICE}: responding to PING"

echo "All services are ready."
