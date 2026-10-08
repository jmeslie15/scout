# Onboarding: fresh Apple Silicon machine to a green build

This is the tested path from a clean Mac to Scout running with a passing test suite. Every command
below was executed on this codebase; where a step has a trap that cost real time, the trap is
called out with what actually happens when you hit it.

Target hardware is an Apple Silicon Mac Mini with **16 GB** (see
[ADR 0001](adr/0001-initial-hardware-target.md)). It also works on any Apple Silicon Mac.

---

## 1. Prerequisites

| Tool | Version verified | Install |
|---|---|---|
| Xcode Command Line Tools | — | `xcode-select --install` (git comes from here) |
| Homebrew | 7.x | <https://brew.sh> |
| `uv` | 0.12.4 | `brew install uv` |
| Node.js | **20.20.2** | `brew install nvm`, then `nvm install 20` — see the trap below |
| pnpm | 9.x | `corepack enable && corepack prepare pnpm@9 --activate` |
| Docker Desktop | 28.x | <https://docker.com> — **must be running**, not merely installed |

Python is **not** a prerequisite you install. `.python-version` pins 3.12 and `uv` fetches its own
interpreter, so the version you develop against matches the container image.

### Trap: the default `node` on PATH may be v16

Vite 5 and pnpm 9 both refuse to run on Node 16. On this machine `node --version` reported
`v16.20.2` while a usable 20.20.2 was present under nvm. Check before you start:

```bash
node --version          # must be v18+ ; v20 recommended
nvm install 20 && nvm use 20
```

If `node --version` prints v16 and `nvm` is not on your shell path, either add nvm to your profile
or prefix the dashboard commands with the explicit bin directory.

---

## 2. Clone and bootstrap

```bash
git clone <the repo url> scout
cd scout

# Python workspace + dashboard dependencies.
# --all-packages is REQUIRED. See the trap below.
uv sync --all-packages

corepack enable && corepack prepare pnpm@9 --activate
cd apps/dashboard && pnpm install && cd ../..
```

`make bootstrap` does the Python half and warns rather than failing if the dashboard half cannot
run, so it is safe to use as a first step.

### Trap: a plain `uv sync` does not install the apps

The workspace root declares only `packages/*` as dependencies. `apps/api`, `apps/orchestrator`, and
`apps/worker` are workspace *members*, so a bare `uv sync` leaves FastAPI, uvicorn, and redis out of
the environment and `make api` fails with an import error. Always use `--all-packages`. The
Dockerfiles already do.

---

## 3. Start the datastores

```bash
# Docker Desktop must be running. If it is not, this fails quickly and clearly.
docker info >/dev/null && echo "docker up" || open -a Docker

make up          # postgres + redis only — the fast path
make ps          # both should read (healthy)
```

Ports are deliberately non-default so they do not collide with a Postgres or Redis you already
run: **PostgreSQL on 5433**, **Redis on 6380**.

---

## 4. Create `.env`

`make up` and `make bootstrap` create `.env` from `.env.example` on first run if it is missing.
The defaults run fully offline — fake GitHub client, fake LLM provider — so you can bring the whole
platform up with **no external accounts and no API keys**.

`.env` is gitignored. Never commit it.

### Trap: Alembic needs `SCOUT_DATABASE_URL` exported

`migrations/env.py` reads `os.environ` directly. It does **not** load `.env`. So migrations fail
with `error: SCOUT_DATABASE_URL is not set` unless you export it, even when `.env` exists:

```bash
export SCOUT_DATABASE_URL='postgresql+asyncpg://scout:scout@localhost:5433/scout'
export SCOUT_REDIS_URL='redis://localhost:6380/0'

make migrate                   # or: uv run alembic -c migrations/alembic.ini upgrade head
uv run alembic -c migrations/alembic.ini check    # expect: No new upgrade operations detected
```

Put those two exports in your shell profile, or in a `.envrc` if you use direnv. This is the single
most common first-run failure.

---

## 5. Verify the build is green

```bash
make verify        # lint + typecheck + all hermetic tests
```

Expected on a healthy checkout: **ruff clean, `ruff format` clean, `mypy --strict` clean across
~169 source files, and ~1516 tests passing.** This set needs no Docker and no database.

Then the dependent suites:

```bash
make up
make test-integration     # ~157 tests, needs PostgreSQL
make test-e2e             # 3 tests: the end-to-end vertical slice
```

### Trap: integration tests SKIP when the database is down — they do not fail

Every integration and e2e test skips cleanly with a message when Postgres is unreachable, which is
correct for a developer without Docker but means **a green run can be green because everything was
skipped**. Always read the count. If you see `N skipped` where you expected `N passed`, the
database is not reachable.

Use the timeout guard so a hang is a failure rather than a two-hour wait:

```bash
uv run pytest -m integration --timeout=300
```

This matters: an earlier run of the idempotency suite hung for **1h47m** on a real Postgres
deadlock before being fixed. Without `--timeout` it would simply have sat there.

---

## 6. Run the processes

In separate terminals, or via `make up-app` for the containerised full stack:

```bash
make api            # http://localhost:8000  — GET /health, GET /ready
make orchestrator   # consumes the job queue
make worker         # executes stages
make dashboard      # http://localhost:5173
```

### Trap: port 8000 is a popular squatter

On a machine with other projects running, port 8000 may already be taken and `make api` will fail
to bind. Check first:

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
```

Use a different port if needed: `uv run python -m scout_api --port 8010`.

Verify the API is genuinely up rather than merely listening:

```bash
curl -s localhost:8000/health | jq          # {"status":"ok",...}
curl -s localhost:8000/ready  | jq          # per-dependency detail
curl -s localhost:8000/api/v1/tools | jq '.items | length'   # 13
```

`/ready` reports each dependency separately and returns `degraded` rather than a 5xx, so it tells
you *which* thing is down.

---

## 7. Optional: the sandbox isolation backend

Development defaults to `SCOUT_SANDBOX_BACKEND=local`, which runs repository code as your user on
the host. That is fine for fixtures and **not** a security boundary. For real repository code, build
the container image and switch:

```bash
docker build -f infra/docker/sandbox-python.Dockerfile -t scout-sandbox-python:3.12 .
export SCOUT_SANDBOX_BACKEND=docker

bash scripts/sandbox_smoke.sh
```

That script asserts two things and fails if either is false: a trivial command runs inside the
sandbox, and network egress is **denied**. Verified output:

```
sandbox-ok: Python 3.12.15 node v20.20.2 git 2.47.3
egress-denied: OSError: [Errno 101] Network is unreachable
Sandbox smoke test passed: untrusted code runs, network is isolated.
```

The factory refuses `local` when `SCOUT_ENV=production`.

---

## 8. Where things are

```
apps/api              control plane: webhooks, jobs, questions, approvals
apps/orchestrator     job state machine, stage dispatch, lease reclaim, reporting
apps/worker           disposable execution: checkout, sandbox, tool runs
apps/dashboard        operator UI (React + Vite, port 5173)
packages/domain       pure model: state machine, evidence hierarchy, recommendation gates
packages/persistence  18 tables, repositories, unit of work, Redis Streams queue
packages/github       GitHub App auth, webhook verification, check runs
packages/sandbox      isolation: argv-only commands, path defence, docker + local backends
packages/tools        13 deterministic tools and the artifact store
packages/llm          provider-agnostic gateway, prompt versioning, accounting
packages/knowledge    engineering memory and ranking
packages/notifications severity routing and dedupe
prompts/<role>/v1.md  versioned agent prompts (see prompts/README.md for the variable contract)
migrations/           Alembic; check gives no drift
docs/                 architecture, ADRs, runbook, security model, policies
```

Read [OPERATIONS.md](OPERATIONS.md) for the runbook — failure modes with diagnosis, backup and
restore, safe retry — and [ARCHITECTURE.md](ARCHITECTURE.md) for the design.

---

## 9. What is not wired yet

Know this before you interpret a green build as a working product:

- **No reasoning agent is scheduled.** Only repository analysis calls a model, and it falls back to
  a deterministic narrative on any failure. `packages/agents` declares the roster and capability
  model; the code-review, security, test, feature-verification, and investigation roles have prompts
  and no call site.
- **Feature verification cannot verify stated intent.** It extracts acceptance criteria and records
  every prose-derived one `not_verified`, which correctly forces `REQUIRES_HUMAN_REVIEW`. The agent
  that would derive an executable procedure is not built.
- **No live GitHub or model-provider call has been made.** The GitHub and LLM suites are offline
  against `respx` and a fake provider; vendor contracts are pinned against documented schemas but
  unverified against the real APIs.
- **No autonomous merge or deploy, by design.** Scout recommends; a human authorizes.

[ROADMAP.md](ROADMAP.md) has per-milestone status with the evidence for each.

---

## 10. Definition of done for a fresh machine

You are set up when all of these hold:

```bash
node --version                                   # v18+ (v20 recommended)
uv run python -c "import scout_api, scout_orchestrator, scout_worker; print('imports ok')"
docker compose -f infra/compose/docker-compose.yml ps   # postgres + redis (healthy)
make verify                                      # lint + typecheck + ~1516 hermetic tests
uv run alembic -c migrations/alembic.ini check   # No new upgrade operations detected
curl -s localhost:8000/health                    # after `make api`
uv run pytest -m integration --timeout=300       # ~157 passed (NOT skipped)
uv run pytest tests/e2e --timeout=300            # 3 passed
```
