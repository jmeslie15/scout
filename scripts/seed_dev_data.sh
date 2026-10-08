#!/usr/bin/env bash
# =============================================================================
# seed_dev_data.sh — insert one repository and one sample job for local work.
#
# Idempotent: both rows use fixed UUIDs and `ON CONFLICT DO NOTHING`, so running
# this repeatedly converges on the same state.
#
# The rows are inserted with psql *inside the postgres container*, so no host
# psql client is required.
#
# Prerequisites: `make up` (datastores running) and `make migrate` (schema).
#
# Environment (column names are overridable to insulate this script from schema
# churn owned by the persistence package):
#   REPOSITORY_TABLE (default repositories), REPOSITORY_PK (default id)
#   JOB_TABLE (default jobs), JOB_PK (default id), JOB_REPOSITORY_FK (default repository_id)
#   JOB_STATE (default queued)
#   SCOUT_DATABASE_USER/DATABASE/NAME via POSTGRES_USER/POSTGRES_DB
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/infra/compose/docker-compose.yml}"
PG_SERVICE="${PG_SERVICE:-postgres}"
POSTGRES_USER="${POSTGRES_USER:-scout}"
POSTGRES_DB="${POSTGRES_DB:-scout}"

# Fixed identifiers keep the seed idempotent across runs.
REPOSITORY_ID="${REPOSITORY_ID:-11111111-1111-4111-8111-111111111111}"
JOB_ID="${JOB_ID:-22222222-2222-4222-8222-222222222222}"

REPOSITORY_TABLE="${REPOSITORY_TABLE:-repositories}"
REPOSITORY_PK="${REPOSITORY_PK:-id}"
JOB_TABLE="${JOB_TABLE:-jobs}"
JOB_PK="${JOB_PK:-id}"
JOB_REPOSITORY_FK="${JOB_REPOSITORY_FK:-repository_id}"
JOB_STATE="${JOB_STATE:-queued}"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
    echo "ERROR: compose file not found: ${COMPOSE_FILE}" >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: cannot talk to the Docker daemon. Is Docker running?" >&2
    exit 1
fi

# Refuse to guess: if migrations have not run, say so instead of half-seeding.
if ! docker compose -f "${COMPOSE_FILE}" exec -T "${PG_SERVICE}" \
    psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    -tAc "SELECT to_regclass('public.${REPOSITORY_TABLE}') IS NOT NULL AND to_regclass('public.${JOB_TABLE}') IS NOT NULL" \
    | grep -q '^t$'; then
    echo "ERROR: tables '${REPOSITORY_TABLE}'/'${JOB_TABLE}' are missing." >&2
    echo "       Run 'make migrate' first, then re-run this script." >&2
    exit 1
fi

echo "Seeding ${REPOSITORY_TABLE} + ${JOB_TABLE} in ${POSTGRES_DB}..."

docker compose -f "${COMPOSE_FILE}" exec -T "${PG_SERVICE}" \
    psql -v ON_ERROR_STOP=1 \
        -v repo_id="${REPOSITORY_ID}" \
        -v job_id="${JOB_ID}" \
        -v job_state="${JOB_STATE}" \
        -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <<SQL
BEGIN;

-- One repository under verification.
INSERT INTO public.${REPOSITORY_TABLE} (${REPOSITORY_PK}, name, url)
VALUES (:'repo_id'::uuid, 'scout-sample', 'https://github.com/edrevel-ai/scout-sample')
ON CONFLICT (${REPOSITORY_PK}) DO NOTHING;

-- One sample job attached to it.
INSERT INTO public.${JOB_TABLE} (${JOB_PK}, ${JOB_REPOSITORY_FK}, state)
VALUES (:'job_id'::uuid, :'repo_id'::uuid, :'job_state')
ON CONFLICT (${JOB_PK}) DO NOTHING;

COMMIT;
SQL

echo "Seed complete: repository=${REPOSITORY_ID} job=${JOB_ID}"
