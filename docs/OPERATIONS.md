# Scout Operations Runbook

How to stand Scout up, run its processes, diagnose failures, and recover state. Written for the
initial single-machine deployment target (Apple Silicon Mac Mini, M1, 16 GB —
[ADR 0001](adr/0001-initial-hardware-target.md)).

Related: [ARCHITECTURE.md](ARCHITECTURE.md) · [ROADMAP.md](ROADMAP.md) ·
[ENGINEERING_POLICIES.md](ENGINEERING_POLICIES.md) · [SECURITY.md](SECURITY.md).

> **The `Makefile` is owned by another workstream.** It exists and is the supported developer
> interface, but treat its target names as a moving contract: every `make` target below is followed
> by the underlying command it runs, and the command is the authoritative form.
>
> **Status and snapshot warning.** The substrate is real: `Makefile`, `infra/compose/docker-compose.yml`,
> the Dockerfiles, `scripts/`, `.github/workflows/ci.yml`, and `tests/conftest.py` all exist, as do the
> domain layer, the persistence schema/mapping/repositories, the sandbox, the GitHub client, the LLM
> gateway, the knowledge store, the notification channels, the tool and artifact stores, the API
> routers, and the dashboard scaffold. Three things are not built and are marked **planned** below:
> the Alembic environment (`migrations/` is empty, so `make migrate` fails), `scout_api/main.py` (so
> the routers are unreachable and `make api` fails), and the orchestrator and worker entry points.
> The tree was changing rapidly while this was written — `apps/dashboard/` and the API routers
> appeared mid-pass — so verify any "does not exist yet" claim before relying on it.

---

## 1. Prerequisites and versions

| Tool | Version | Why / notes |
|---|---|---|
| Python | 3.12+ | Pinned by `.python-version` (`3.12`) and `requires-python = ">=3.12"`; `uv` fetches it. |
| [uv](https://docs.astral.sh/uv/) | 0.4+ (CI pins `0.5.11`) | Owns the Python workspace and `uv.lock`. All Python commands run through it. |
| Node.js | 20 LTS | Dashboard only; CI uses `actions/setup-node` with `node-version: 20`. |
| pnpm | 9 | Dashboard only; CI uses `pnpm/action-setup` with `version: 9`. |
| Docker + Compose | v2 | PostgreSQL, Redis, and the sandbox image. `docker compose version` must report v2. |
| `make` | any | The supported entry points. |
| PostgreSQL client | 16+ (optional) | `psql`/`pg_dump`/`pg_restore` for §8 and §9; the scripts use `docker compose exec` instead. |
| `jq` | any (optional) | The README's health-check examples pipe through it. |

Host sizing and why: [ADR 0001](adr/0001-initial-hardware-target.md). The concurrency defaults
(`SCOUT_MAX_CONCURRENT_JOBS=2`, `SCOUT_MAX_CONCURRENT_SANDBOXES=1`) assume a 16 GB host with one
sandbox at a time.

---

## 2. First-time setup

```bash
git clone <repository-url> scout
cd scout
make bootstrap          # creates .env from .env.example, then installs dependencies
```

`make bootstrap` depends on a `.env` target that copies `.env.example` to `.env` only if `.env` does
not already exist (the template contains no secrets). Underlying commands:

```bash
# 1. Create .env if absent
[ -f .env ] || cp .env.example .env

# 2. Python workspace: resolve uv.lock and install every member
uv sync --all-packages

# 3. Dashboard dependencies, guarded — skipped while apps/dashboard/package.json is absent
corepack enable && ( cd apps/dashboard && pnpm install )
```

`.env.example` holds no credentials and the defaults run **fully offline**:

- `SCOUT_GITHUB_APP_ID` and `SCOUT_GITHUB_WEBHOOK_SECRET` blank → the in-memory fake GitHub client.
- `SCOUT_LLM_DEFAULT_PROVIDER=fake` → no model provider account required.
- `SCOUT_NOTIFICATIONS_ENABLED=false` → no outbound notifications.
- `SCOUT_SANDBOX_BACKEND=local` → no container runtime required for development.
- `SCOUT_OTEL_ENABLED=false` → no collector required.

Never commit `.env`: it is gitignored (`.env`, `.env.*`, with `!.env.example`), and secrets are
supplied through the environment only — never through prompts, repository configuration, or
agent-visible context. See [SECURITY.md](SECURITY.md).

**Verify the toolchain.** `make lint` (ruff format check + ruff check) and `make typecheck` (mypy
strict) are the fastest way to confirm the workspace resolved correctly.

---

## 3. Starting the stack

Two modes. The fast path runs the datastores in containers and the Python processes on the host;
the full path runs everything in containers behind the Compose `app` profile.

### 3.1 Datastores (fast path)

```bash
make up
```

runs:

```bash
docker compose -f infra/compose/docker-compose.yml up -d postgres redis
bash scripts/wait_for_services.sh
```

`scripts/wait_for_services.sh` polls PostgreSQL and Redis **from inside their containers** until both
accept connections, and exits non-zero with a clear message on timeout (`TIMEOUT`, default 60 s).
Override `COMPOSE_FILE`, `PG_SERVICE`, `REDIS_SERVICE`, `POSTGRES_USER`, and `POSTGRES_DB` through
the environment, never by editing the script.

| Service | Container | Host port | Connection string | Notes |
|---|---|---|---|---|
| `postgres` | `scout-postgres` | **5433** | `SCOUT_DATABASE_URL=postgresql+asyncpg://scout:scout@localhost:5433/scout` | `postgres:16-alpine`, named volume `scout-postgres-data`, `pg_isready` healthcheck. Host port 5433 because 5432 is frequently already bound on macOS. |
| `redis` | `scout-redis` | **6380** | `SCOUT_REDIS_URL=redis://localhost:6380/0` | `redis:7-alpine`, named volume `scout-redis-data`, started with `--appendonly yes` so queue entries survive a container restart. |

PostgreSQL and Redis credentials in the Compose file are local development values (`scout`/`scout`);
every real secret is read from the environment.

Other datastore targets:

```bash
make ps      # docker compose --profile app ps
make logs    # docker compose --profile app logs -f --tail=100
make down    # docker compose --profile app down --remove-orphans   (keeps volumes)
```

### 3.2 Full stack in containers

```bash
make up-app      # docker compose -f infra/compose/docker-compose.yml --profile app up -d --build
                 # then scripts/wait_for_services.sh
make down-app    # docker compose --profile app down --remove-orphans
```

Inside containers the connection strings are deliberately different from `.env`: the Compose
environment overrides them to the service names and internal ports
(`postgresql+asyncpg://scout:scout@postgres:5432/scout`, `redis://redis:6379/0`), because
`localhost:5433` is only correct for host-run processes.

The `worker` service is the only application service that mounts `/var/run/docker.sock`, because it
is the only one that launches sandboxes. It is effectively host root. See
[SECURITY.md](SECURITY.md) before changing that.

### 3.3 Migrations

```bash
make migrate     # uv run alembic -c migrations/alembic.ini upgrade head
make migration m="add job evidence table"   # autogenerate
make downgrade                              # rev=-1 by default
make db-reset                               # downgrade base, then upgrade head
```

**Planned.** `migrations/alembic.ini` and the Alembic environment do not exist yet — `migrations/` is
empty. These targets fail today; the schema itself is declared in
`packages/persistence/scout_persistence/tables.py` and the mapping and repositories are in place, but
no revision has ever been generated and the schema has never been applied to a database. CI's
migration step is guarded by `if [ -f migrations/alembic.ini ]` and skips rather than failing, so a
green CI build does not yet prove the migration works.

Migrations are never applied automatically at process start: a process that silently alters the
schema on boot makes concurrent deploys unsafe.

### 3.4 Application processes

The Makefile runs each process in the foreground with the reload/`-m` behaviour noted below.

| Target | Underlying command | Status |
|---|---|---|
| `make api` | `uv run uvicorn scout_api.main:app --host 0.0.0.0 --port 8000 --reload` | **Planned:** `scout_api.main` does not exist yet. |
| `make orchestrator` | `uv run python -m scout_orchestrator` | **Planned:** no `scout_orchestrator/__main__.py` yet. |
| `make worker` | `uv run python -m scout_worker` | **Planned:** no `scout_worker/__main__.py` yet. |
| `make dashboard` | `pnpm dev` (Vite) in `apps/dashboard`, guarded by `package.json` existing | Available: the dashboard is scaffolded with `dev`/`build`/`typecheck`/`lint`/`test` scripts. It renders nothing useful until the API serves. |

Start order: PostgreSQL and Redis → migrate → API → orchestrator → worker → dashboard. `SCOUT_PUBLIC_URL=http://localhost:8000`
is the address Scout reports for itself; it must be reachable from the browser and from GitHub.

Planned topology:

```mermaid
flowchart LR
    PG[("PostgreSQL :5433")] --> MIG["alembic upgrade head"]
    MIG --> API["apps/api :8000"]
    RD[("Redis :6380")] --> ORCH["apps/orchestrator"]
    API --> ORCH
    ORCH --> WK["apps/worker"]
    API --> DASH["apps/dashboard :5173"]
```

---

## 4. Health and readiness

Two endpoints, both documented in the README:

| Endpoint | Purpose | Expected |
|---|---|---|
| `GET /health` | **Liveness.** The process is up and its event loop is responsive. Must not check dependencies. | Always `200` while the process runs. A non-`200` or a timeout means the process itself is wedged and should be restarted. |
| `GET /ready` | **Readiness.** The process can serve real work. Must report dependency status. | `200` only when its dependencies are reachable and usable; otherwise a non-`200` with a per-dependency breakdown. |

```bash
curl -s localhost:8000/health | jq
curl -s localhost:8000/ready  | jq
```

The Compose `api` healthcheck already depends on this contract:

```
python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).status == 200 else 1)"
```

Readiness must name the failing dependency rather than returning a bare error. The dependency set:

| Process | Readiness dependencies |
|---|---|
| `apps/api` | PostgreSQL reachable and migrated; Redis reachable; GitHub App credentials valid (or the fake client active in development) |
| `apps/orchestrator` | PostgreSQL reachable; Redis reachable and the job stream/consumer group present |
| `apps/worker` | PostgreSQL reachable; sandbox backend usable (`docker version` succeeds, or the local backend is selected); configured sandbox image present (`docker image inspect`) |

The split is deliberate: a process whose database is briefly unavailable should be taken out of
rotation, not restarted, because restarting will not fix the database.

**Status.** The two probes are implemented in `apps/api/scout_api/routers/health.py`: `/health`
deliberately touches no dependency, and `/ready` probes each dependency and reports it individually,
returning `200` with a `degraded` status rather than a 5xx when a non-critical dependency is down.
They are not reachable yet, because `apps/api/scout_api/main.py` — the application object that would
mount the routers — does not exist, so `make api` fails before binding a port.

---

## 5. Reading logs

| Variable | Effect |
|---|---|
| `SCOUT_LOG_LEVEL=INFO` | Log level for the structured logger. |
| `SCOUT_LOG_JSON=false` | `false` for human-readable local output; `true` for line-delimited JSON. **JSON is forced on whenever `SCOUT_ENV` is not `development`, `test`, or `local`** — machine-readable logs are what make production incidents diagnosable, and that must not depend on remembering a flag. |
| `SCOUT_OTEL_ENABLED=false` | Enables OpenTelemetry traces and metrics. |
| `SCOUT_OTEL_EXPORTER_ENDPOINT=http://localhost:4318` | OTLP/HTTP endpoint. |
| `SCOUT_OTEL_SERVICE_NAME=scout-api` | Service name attached to every log event and reported to the collector. `.env.example` sets it to `scout-api`; the code falls back to `scout` when it is unset, and the orchestrator and worker need their own values so their logs can be separated after collection. |

`scout-observability` (`packages/observability/scout_observability/`) provides the pipeline shared by
every process:

- **Correlation context** via `contextvars` — `bind_context`, `job_context`, `get_correlation_id`,
  `get_job_id`. Values survive `await` points, so every log line inside a job's task carries its
  correlation id. `VerificationJob.correlation_id` is the key; `AuditEvent` carries it too.
- **Redaction runs as the last processor before rendering**, so it sees the final event dictionary.
  Spec sections 38 and 55 forbid secrets in logs and in model-request records, and a rule that
  depends on every developer remembering it is not a rule — it is a processor that runs on every
  event. `redact_mapping` is exposed for reuse and `REDACTED` is the stable placeholder.

Practical reads:

```bash
make logs                                     # all compose services, tail 100, follow
docker compose -f infra/compose/docker-compose.yml logs -f postgres
docker compose -f infra/compose/docker-compose.yml logs -f redis

# Follow one job end to end (with SCOUT_LOG_JSON=true). --no-log-prefix is required
# because `docker compose logs` otherwise prefixes each line with the service name,
# which is not valid JSON:
docker compose -f infra/compose/docker-compose.yml logs -f --no-log-prefix \
  | jq -c 'select(.correlation_id == "<job correlation_id>")'
```

Prefer filtering by `correlation_id` over message text. Message wording is not a stable interface;
identifiers are.

---

## 6. Common failure modes

### 6.1 PostgreSQL not ready or unreachable

**Symptoms.** Readiness fails; connection errors naming
`postgresql+asyncpg://scout:scout@localhost:5433/scout`; `make migrate` fails to connect.

**Diagnose.**

```bash
make ps
docker compose -f infra/compose/docker-compose.yml logs --tail=100 postgres
docker compose -f infra/compose/docker-compose.yml exec postgres pg_isready -U scout -d scout
bash scripts/wait_for_services.sh          # re-poll both datastores
```

**Causes and fixes.**

| Cause | Fix |
|---|---|
| Container still initialising | Wait for the healthcheck (`interval 5s`, `retries 10`, `start_period 10s`); readiness recovers on its own. |
| Port collision on 5433 | Another process owns the port. Change the Compose mapping *and* `SCOUT_DATABASE_URL` together. |
| Wrong credentials in `.env` | The Compose defaults are `scout`/`scout`/`scout`. A partially copied `.env` is the most common cause; `make bootstrap` only creates `.env` when it is absent, so a stale one persists. |
| Volume from an older schema | Check `alembic current` before assuming corruption; usually this is an unapplied migration. |
| Host memory pressure | Check swap and memory pressure; PostgreSQL being killed under memory pressure is the reason the hardware decision is what it is ([ADR 0001](adr/0001-initial-hardware-target.md)). |

### 6.2 Redis unavailable

**Symptoms.** Readiness fails; the orchestrator cannot claim jobs; jobs sit in `QUEUED` and never
progress.

**Diagnose.**

```bash
make ps
docker compose -f infra/compose/docker-compose.yml logs --tail=100 redis
docker compose -f infra/compose/docker-compose.yml exec redis redis-cli ping          # expect PONG
docker compose -f infra/compose/docker-compose.yml exec redis redis-cli info consumers
docker compose -f infra/compose/docker-compose.yml exec redis redis-cli info persistence
```

**Causes and fixes.** Port 6380 collision; the container restarted and the AOF file is still loading
(check `rdb_last_load_keys_expired`/`aof_loading` in `info persistence`); the orchestrator's consumer
group was never created.

**Important.** Redis is **not** the system of record. `verification_jobs` and `job_stages` in
PostgreSQL hold verification state, and the lease reclaim in §6.6 is what reconstructs work. Losing
Redis loses scheduling state, not verification state; the recovery path is the lease sweep, not a
Redis restore.

### 6.3 Sandbox image missing

**Symptoms.** `SandboxBackendUnavailable` from `DockerSandbox.start()` with a message naming the
image; tool runs fail as infrastructure errors with `ToolResult.infrastructure_failure=True`. Because
this is infrastructure rather than a code defect, it must **not** appear as a finding against the
pull request.

**Diagnose.**

```bash
echo "$SCOUT_SANDBOX_IMAGE"                   # default: scout-sandbox-python:3.12
docker image inspect "$SCOUT_SANDBOX_IMAGE"
docker version --format '{{.Server.Version}}' # is the daemon reachable from this shell?
```

**Fix.** Build the image exactly as the backend's error message instructs:

```bash
docker build -f infra/docker/sandbox-python.Dockerfile -t "$SCOUT_SANDBOX_IMAGE" .
```

or run the smoke test, which builds it and then asserts the hardening:

```bash
scripts/sandbox_smoke.sh      # or: make test-sandbox
```

`scripts/sandbox_smoke.sh` builds the untrusted-code image, runs a trivial command under the full
hardening flags, and **fails if network egress succeeds** (probe defaults `SANDBOX_PROBE_HOST=1.1.1.1`,
`SANDBOX_PROBE_PORT=443`).

Other causes: the Docker daemon is not running or the current user cannot reach it; the configured
tag changed without a rebuild; the Docker VM is out of disk. `SCOUT_SANDBOX_BACKEND=local` avoids
Docker entirely for development, at the cost of isolation — it is **not** a security boundary.

### 6.4 Model provider rate limit

**Symptoms.** Agent runs fail or are delayed; `ModelRequestStatus.RATE_LIMITED` recorded on the
`model_requests` row with an error; retries visible as increasing `attempt` on the same logical
request.

**Diagnose.**

```bash
psql "postgresql://scout:scout@localhost:5433/scout" -c \
  "select id, job_id, agent, provider, model, status, attempt, latency_seconds, error
     from model_requests where status <> 'SUCCEEDED' order by created_at desc limit 20;"
```

Check `SCOUT_LLM_MAX_CONCURRENT_CALLS=4` and `SCOUT_LLM_REQUEST_TIMEOUT_SECONDS=120` as well.

**Behaviour to expect.** Rate limits are transient upstream conditions and are classified retryable;
they are retried with exponential backoff and jitter (`DEFAULT_RETRY_POLICY`: 3 attempts, 2 s base,
×2, 5 min ceiling, 15% jitter). If the retry budget is exhausted, the job fails with a recorded
reason rather than hanging.

**Fixes.** Lower `SCOUT_LLM_MAX_CONCURRENT_CALLS` to reduce burst rate; raise the provider quota;
switch the affected role's provider; or set `SCOUT_LLM_DEFAULT_PROVIDER=fake` to prove the rest of
the pipeline is healthy while the provider is degraded. A rate limit is an infrastructure failure and
must be recorded as one, never as a code finding.

### 6.5 GitHub webhook signature mismatch

**Symptoms.** The API rejects deliveries; no job is created; GitHub's delivery log shows a non-2xx
response. This is a security control working: an unsigned or mis-signed delivery must be rejected,
and the rejection must not distinguish "missing" from "wrong".

**Diagnose.**

1. GitHub → repository → Settings → Webhooks → the Scout hook → **Recent Deliveries**: read the
   response code and body.
2. Confirm the API's `SCOUT_GITHUB_WEBHOOK_SECRET` matches the webhook's secret in GitHub.
3. Confirm the body is verified **before** parsing. A proxy or framework that re-serialises the body
   before HMAC verification breaks every signature; the raw bytes are what was signed.

**Fixes.** Re-set the secret in both places; if it was rotated, update `.env` and restart the API
(secrets are environment-supplied, never in repository configuration). Re-deliver from GitHub
afterwards. Each delivery carries a delivery id, stored `UNIQUE` in `webhook_events.delivery_id` —
that constraint, not an application read, is what stops a redelivery from creating a second job.
Redelivering a processed event produces a `DUPLICATE` receipt and no second `verification_jobs` row.

### 6.6 Job stuck in a non-terminal state, and how the lease reclaims it

**Symptoms.** A job stops progressing. Its `status` is one of the active statuses
(`RECEIVED`, `QUEUED`, `PREPARING`, `ANALYZING`, `REVIEWING`, `SECURITY_SCANNING`, `TESTING`,
`FEATURE_VERIFYING`, `INVESTIGATING`) and does not change. `READY_FOR_REVIEW` and
`WAITING_FOR_HUMAN` are also non-terminal and are *waiting by design*: `WAITING_FOR_HUMAN` means a
question is open and is not stuck. `FAILED`, `CANCELLED`, and `COMPLETED` are not this failure mode
(`FAILED` is a resting state; see §7).

**Diagnose.** The tables and columns below are the declared schema (`tables.py`); queries work as
soon as migrations exist.

```bash
DSN="postgresql://scout:scout@localhost:5433/scout"

# Non-terminal jobs, with lease state and retry accounting
psql "$DSN" -c "
  select id, status, attempt, max_attempts, lease_owner, lease_expires_at,
         now() > lease_expires_at as lease_expired, failure_reason, correlation_id
    from verification_jobs
   where status not in ('COMPLETED','CANCELLED','FAILED')
   order by created_at;"

# Unresolved stage attempts for one job (the stuck point)
psql "$DSN" -c "
  select stage, outcome, attempt, started_at, completed_at, error, skip_reason
    from job_stages
   where job_id = '<job_id>'
   order by created_at;"
```

Work through these in order:

1. **Is a lease held?** `lease_expires_at` in the future means a live owner is working the job. Do
   not intervene; read that owner's logs by `correlation_id`.
2. **Is the lease expired?** `VerificationJob.lease_is_expired(now=...)` is the same predicate as
   `now() > lease_expires_at`. An expired lease is the crash-recovery path: the job is reclaimable
   and `is_resumable` says whether it may be picked up. `verification_jobs` has an index on
   `lease_expires_at` precisely so the sweep is cheap.
3. **Which stage is unresolved?** `next_pipeline_stage(resolved_stages)` returns the first stage
   whose `outcome` is neither `SUCCEEDED` nor `SKIPPED`. A row left `PENDING` or `RUNNING` after its
   owner died is the stuck point — and exactly where the reclaimed job resumes.
4. **Is the retry budget exhausted?** `attempt` versus `max_attempts`; `retries_remaining` is the
   computed remainder. The database also enforces `attempt <= max_attempts`.
5. **Was a sandbox leaked?** `sandbox_runs.native_id` records the container or process-group id for
   troubleshooting leaks; a leaked container may hold the memory the next sandbox needs.

**Recovery.** The lease/reclaim mechanism is the intended answer and it is automatic: the
orchestrator reclaims a job whose lease expired and resumes at the first unresolved stage. If the
orchestrator is down, restore it (§3.4) — it reclaims on startup. Manual intervention is limited to:

- releasing a lease held by a process confirmed dead (never one that is merely slow);
- re-queueing a `FAILED` job (§7);
- cancelling a job with no meaningful path to completion, which requires a recorded reason (the
  `CANCELLED` transition guard rejects a reasonless cancellation).

Never edit a stage's `outcome` to make a job progress. That converts an auditable gap into a silent
one, and the forward guard exists specifically to refuse a stage whose predecessors are unresolved.

---

## 7. Safely retrying a failed job

`FAILED` is a resting state, not a tombstone: re-queueing it is a legal transition
(`FAILED → QUEUED`) guarded by two conditions.

**Preconditions.**

1. **Retry budget remains.** The guard is `retries_remaining > 0`, computed from `max_attempts`
   (default 3) and `attempt`. If the budget is exhausted, do not silently raise `max_attempts` to
   force progress — decide whether the failure is genuinely transient, and if so raise the budget
   deliberately and record why.
2. **A reason is required.** `FAILED → QUEUED` is privileged and audited. State what failed and why a
   retry is justified; an unexplained re-queue makes the audit trail useless in the post-incident
   review.

**What a retry does and does not do.**

- It does **not** repeat completed work. The orchestrator resumes at
  `next_pipeline_stage(resolved_stages)`, so a job that failed in `TESTING` re-enters at `TESTING`,
  not at `PREPARING`.
- It **does** increment attempt accounting on the new `job_stages` row. Stage rows are append-only, so
  the failed attempt remains visible next to the successful one.
- It **does** re-run anything unresolved, including checkout if `PREPARING` never resolved.

**Procedure.**

1. Read `verification_jobs.failure_reason` and the failed stage's `error`. Confirm the failure class:
   a `NON_RETRYABLE` failure (invalid repository, invalid configuration, authentication failure,
   compilation failure, test failure) will fail again. Retrying a test failure is not a retry; it is
   a request to be told the same thing twice.
2. Confirm the environment fault is cleared — PostgreSQL and Redis ready (§4), sandbox image present
   (§6.3), provider quota restored (§6.4).
3. Re-queue with a reason through the operator surface; write an `audit_events` row with action
   `job.transition`. The retry endpoint is **planned** (`apps/api` has no routes yet); the domain
   action is a validated `FAILED → QUEUED` transition with
   `TransitionContext(retries_remaining=..., reason=...)`.
4. Watch the job's `correlation_id` through the pipeline (§5).

**When not to retry.** If the failure was a genuine problem in the code under test, the correct
action is a new commit and therefore a **new job** (spec section 62) — a job is immutable in identity,
and `verification_jobs` is unique per `(repository_full_name, pull_request_number, head_sha)`, which
is what keeps reports and approvals meaningful. If the failure was environment-only and unresolved,
fixing the environment *is* the retry.

---

## 8. Backup and restore of PostgreSQL

PostgreSQL holds the state of record: jobs, stages, findings, evidence locators, questions,
approvals (once the table exists), audit events, and artifact **locators**. The bytes live in the
artifact store (§9). A database backup without the matching artifact store restores a set of pointers
to blobs that may no longer exist, so back them up as a pair.

**Backup (logical, custom format).**

```bash
STAMP=$(date +%Y%m%dT%H%M%S)
docker compose -f infra/compose/docker-compose.yml exec -T postgres \
  pg_dump -U scout -d scout --format=custom > "scout-${STAMP}.dump"
```

With a host client, the equivalent is
`pg_dump "postgresql://scout:scout@localhost:5433/scout" --format=custom --file="scout-${STAMP}.dump"`.

Custom format is chosen over plain SQL because it restores selectively and in parallel and carries
enough metadata to be verified.

**Physical backup.** `pg_basebackup` is the right tool if point-in-time recovery is required. Scout's
initial target does not run WAL archiving, so PITR is **not** available today; a logical dump is the
supported mechanism.

**What is *not* in the dump.** The audit log *is* in the dump — but it is append-only with no update
or delete path, so a restore that rolls the database back also rolls the audit log back. Preserve the
dumps themselves; they are the out-of-band record of what the audit log contained.

**Restore.**

```bash
# Stop API, orchestrator, and worker first: nothing should write during a restore.
make down-app

docker compose -f infra/compose/docker-compose.yml exec -T postgres \
  createdb -U scout scout_restore
docker compose -f infra/compose/docker-compose.yml exec -T postgres \
  pg_restore -U scout -d scout_restore --clean --if-exists --no-owner < "scout-<stamp>.dump"
```

Restoring into a differently named database is deliberate: it lets you verify the dump before
pointing the platform at it. Check that the Alembic revision matches what the processes expect, then
swap by updating `SCOUT_DATABASE_URL` and restarting.

**Verify a backup before you need it.** A dump that has never been restored is not a backup. Restore
the most recent dump into a scratch database and confirm that a known job and its stage rows are
present. Record the check.

**Retention.** Verification reports are the long-lived evidence record: `DEFAULT_RETENTION` keeps
`VERIFICATION_REPORT` for 2 555 days (7 years), other artifacts for 365 days,
`TOOL_STDOUT`/`TOOL_STDERR` for 90 days, and `LOG` for 30 days. Backups must therefore cover the
report artifacts as well as the rows that locate them.

---

## 9. Artifacts, workspaces, and development scripts

| Path | Contents | Safe to delete? |
|---|---|---|
| `SCOUT_ARTIFACT_ROOT=./var/artifacts` | Content-addressed blobs (SHA-256 keys) — raw tool output, diffs, generated tests, reports | No. Deleting a blob breaks the finding that cites it. Unreferenced blobs are harmless; retention reclaims them via `artifacts.expires_at`. |
| `SCOUT_WORKSPACE_ROOT=./var/workspaces` | Checkout and working directories for sandboxes | Yes, when no job is running. Workspaces are disposable; their output is captured as artifacts. |

Both are gitignored except for `.gitkeep`. A workspace deleted mid-job fails that stage and is
retried; it does not corrupt state, because the checkout is reproducible from the pinned
`CommitRange`. The artifact store is implemented in `packages/tools/scout_tools/artifacts.py`:
`LocalArtifactStore` writes atomically under `SCOUT_ARTIFACT_ROOT` at
`sha256/ab/cd/<digest>`, verifies the digest on read (raising `ArtifactIntegrityError` on a mismatch),
exposes `verify`, and provides `prune_expired` that will not delete a blob still referenced by an
unexpired row. There is no scheduled retention job yet, so expiry is a capability rather than a
running process. `InMemoryArtifactStore` keeps tests hermetic; `S3ArtifactStoreStub` is the
placeholder for the Phase-3 object store.

Helper scripts (`scripts/README.md` documents the full set; each resolves the repository root from
its own path and uses `set -euo pipefail`):

| Script | What it does |
|---|---|
| `scripts/wait_for_services.sh` | Polls PostgreSQL and Redis from inside their containers until both accept connections. Run after `make up` / `make up-app`. |
| `scripts/seed_dev_data.sh` | Idempotently inserts one sample repository and one sample job via `psql`; requires the migrated schema (so it is blocked until §3.3 works). |
| `scripts/sandbox_smoke.sh` | Builds the sandbox image, runs a trivial command under the full hardening flags, and fails if network egress succeeds. |

---

## 10. Routine operational checks

| Check | How | Frequency |
|---|---|---|
| Datastores alive | `make ps`; `scripts/wait_for_services.sh` | Continuous (monitor) |
| Processes live and ready | `curl -s localhost:8000/health` and `/ready` (**planned**) | Continuous (monitor) |
| Lint, types, tests | `make verify` — the same gate CI runs. Note that the tree does **not** currently pass `make lint` (ruff reported 108 errors during the foundation build), so treat a red gate as expected until the owning workstreams clear the backlog. | Before every commit |
| Queue health | `redis-cli info consumers` (pending count and consumer lag) | Daily, once the queue exists |
| Jobs stuck in active states beyond `SCOUT_MAX_JOB_RUNTIME_SECONDS=1800` | The §6.6 queries | Daily |
| Lease reclaim working | Confirm reclaimed jobs resume at the first unresolved stage rather than restarting | After every incident |
| Sandbox image present and current | `docker image inspect "$SCOUT_SANDBOX_IMAGE"`; `scripts/sandbox_smoke.sh` | Before each deploy |
| Database and artifact backup | Restore-test the newest dump (§8) | Weekly, plus before any schema change |
| Disk headroom | Docker VM disk and `SCOUT_ARTIFACT_ROOT` growth against retention | Weekly |
