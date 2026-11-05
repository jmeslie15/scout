# Scout

** AI Scout** — an engineering verification platform that continuously verifies software
changes before they are promoted through the development lifecycle.

> **AI investigates and recommends. Deterministic systems verify. Humans authorize.**

Scout watches GitHub pull requests, builds a machine-readable understanding of the repository and
the change, runs deterministic engineering checks inside an isolated sandbox, applies AI agents for
contextual analysis, verifies the stated feature against acceptance criteria, records the exact
evidence behind every conclusion, and produces a structured verification report for human review.

Scout is **not** an autonomous engineer. It never merges, deploys, or approves its own work.

---

## Status

**Verified working today.** The first vertical slice (spec section 83) runs end to end against a
real git repository with no external accounts:

```text
GitHub PR -> webhook -> create job -> checkout pinned SHA -> run pytest
          -> store artifacts -> generate report -> publish GitHub check
```

| Capability | State | Evidence |
|---|---|---|
| Monorepo, Docker Compose, CI | Done | `make up` brings PostgreSQL + Redis to healthy; CI runs lint, typecheck, tests, integration |
| Domain model, state machine, retry policy | Done | `tests/unit/domain/` — 179 tests including every transition guard |
| PostgreSQL schema + migrations | Done | 18 tables; `alembic check` reports no drift; 149 integration tests |
| GitHub webhook + idempotency | Done | 348 tests; duplicate deliveries proven to create exactly one job under concurrency |
| Job orchestration + worker + lease reclaim | Done | Jobs resume after a crash from their first unresolved stage |
| Sandbox + 13 deterministic tools | Done | 127 tool tests; environment-leak and path-escape refusal tested |
| LLM gateway (provider-agnostic) | Done | 257 tests; offline `FakeProvider` is the default |
| Engineering memory + notifications | Done | 198 tests; routing is deliberately quiet at LOW/INFO |
| Operator dashboard | Done | 10 routes; 75 tests; verified at `http://localhost:5173` |
| **AI review, security, test, and feature-verification agents** | **Not wired** | `packages/agents` declares the roster and capability model; only repository analysis calls a model today |
| **Investigation, notifications-on-failure, enforcement** | **Not wired** | Milestones 9–16 |

Full per-milestone status: [`docs/ROADMAP.md`](docs/ROADMAP.md). System design:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

### What this means in practice

Scout can verify a pull request's *deterministic* properties today: does it compile, do the tests
pass, is the change covered, does a scanner flag anything, is the pinned commit what was
approved. It cannot yet *reason* about a change in context, and it does not verify the stated
feature — feature verification currently reports every criterion as `not_verified`, which
correctly forces `REQUIRES_HUMAN_REVIEW`.

That is the honest state, and the recommendation gates are what make it visible rather than
silent: a change whose feature could not be procedurally verified cannot reach `PASS`.

## Hardware

Initial deployment target: **Apple Silicon Mac Mini, M1, 16 GB RAM**. See
[ADR 0001](docs/adr/0001-initial-hardware-target.md) for why 16 GB matters more than CPU generation.

## Requirements

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12+ | pinned via `.python-version`; `uv` will fetch it |
| [uv](https://docs.astral.sh/uv/) | 0.4+ | Python workspace + lockfile |
| Node.js | 20 LTS | dashboard only |
| pnpm | 9+ | dashboard only |
| Docker + Compose | v2 | PostgreSQL, Redis, sandbox images |

## Quickstart

Setting up a fresh machine — especially the Apple Silicon Mac Mini — follow
[`docs/ONBOARDING.md`](docs/ONBOARDING.md) instead; it covers the prerequisites and the four traps
that cost real time (default Node v16, `uv sync` without `--all-packages`, Alembic needing
`SCOUT_DATABASE_URL` exported, and integration tests skipping rather than failing when Postgres is
down).

```bash
cp .env.example .env          # defaults run fully offline (fake GitHub + fake LLM)
make bootstrap                # install Python + dashboard dependencies
make up                       # start PostgreSQL + Redis
make migrate                  # apply database migrations
make api                      # FastAPI on http://localhost:8000
```

Verify:

```bash
curl -s localhost:8000/health | jq
curl -s localhost:8000/ready  | jq
```

Run the test suite:

```bash
make test          # unit + contract tests (no Docker required)
make test-all      # adds integration tests; requires `make up`
```

## Layout

```
apps/
  api/            control-plane HTTP API (webhooks, jobs, questions, approvals)
  orchestrator/   job state machine, stage sequencing, retries, escalation
  worker/         disposable execution plane (checkout, sandboxed tools, agents)
  dashboard/      React + TypeScript operator UI
packages/
  domain/         pure domain model: identifiers, state machine, findings, evidence
  persistence/    SQLAlchemy schema, repositories, Alembic migrations
  observability/  structured logging, correlation IDs, OpenTelemetry
  github/         GitHub App auth, webhook verification, checks
  sandbox/        isolated execution, resource limits, network policy
  tools/          deterministic tool framework (pytest, ruff, gitleaks, ...)
  llm/            provider-agnostic model gateway
  knowledge/      engineering memory and retrieval
  agents/         reasoning agents with strict context/result contracts
  notifications/  Google Chat, email, severity routing
prompts/          versioned prompt templates
migrations/       Alembic migration environment
infra/            Dockerfiles and Compose stacks
docs/             architecture, ADRs, runbooks
```

## Non-negotiables

These are enforced in code review, not just documented:

1. LLM output is never treated as deterministic evidence.
2. Every finding cites evidence that traces to a tool run, file range, or human answer.
3. Repository code executes only inside a sandbox with no control-plane credentials.
4. Every job is resumable; no in-memory-only state.
5. Every privileged action is written to the audit log.
6. No autonomous merging or deployment.
7. Tests ship with the implementation.

Full policy list: [`docs/ENGINEERING_POLICIES.md`](docs/ENGINEERING_POLICIES.md).

## License

Proprietary —  AI. Internal use only.
