# Scout scripts

Small, focused helpers. Every script uses `set -euo pipefail`, is executable,
and fails loudly rather than continuing on error.

| Script | What it does |
|---|---|
| [`wait_for_services.sh`](wait_for_services.sh) | Polls PostgreSQL and Redis (from inside their containers) until both accept connections; exits non-zero with a clear message on timeout. |
| [`seed_dev_data.sh`](seed_dev_data.sh) | Idempotently inserts one sample repository and one sample job via `psql` for local development; requires the migrated schema. |
| [`sandbox_smoke.sh`](sandbox_smoke.sh) | Builds the untrusted-code sandbox image, runs a trivial command under the full hardening flags, and fails if network egress succeeds. |

Run them from anywhere; each resolves the repository root from its own path.

```bash
scripts/wait_for_services.sh          # after `make up`
scripts/seed_dev_data.sh              # after `make up && make migrate`
scripts/sandbox_smoke.sh              # builds infra/docker/sandbox-python.Dockerfile
```

Override behavior through the environment, never by editing the scripts:

| Variable | Used by | Default |
|---|---|---|
| `COMPOSE_FILE` | `wait_for_services.sh`, `seed_dev_data.sh` | `infra/compose/docker-compose.yml` |
| `PG_SERVICE` / `REDIS_SERVICE` | `wait_for_services.sh` | `postgres` / `redis` |
| `TIMEOUT` / `POSTGRES_USER` / `POSTGRES_DB` | `wait_for_services.sh`, `seed_dev_data.sh` | `60` / `scout` / `scout` |
| `SCOUT_SANDBOX_IMAGE` | `sandbox_smoke.sh` | `scout-sandbox-python:3.12` |
| `SANDBOX_PROBE_HOST` / `SANDBOX_PROBE_PORT` | `sandbox_smoke.sh` | `1.1.1.1` / `443` |
