# Scout

**Edrevel AI Scout** — an engineering verification platform that continuously verifies software
changes before they are promoted through the development lifecycle.

> **AI investigates and recommends. Deterministic systems verify. Humans authorize.**

Scout watches GitHub pull requests, builds a machine-readable understanding of the repository and
the change, runs deterministic engineering checks inside an isolated sandbox, applies AI agents for
contextual analysis, verifies the stated feature against acceptance criteria, records the exact
evidence behind every conclusion, and produces a structured verification report for human review.

Scout is **not** an autonomous engineer. It never merges, deploys, or approves its own work.

---

## Status

Early development. The platform foundation (Milestone 0–2) is being built first, followed by the
first end-to-end vertical slice:

```
GitHub PR -> Webhook -> Create Job -> Checkout -> Run Tests -> Store Results -> Report -> GitHub Check
```

See [`docs/ROADMAP.md`](docs/ROADMAP.md) for milestone status and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for the system design.

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

Proprietary — Edrevel AI. Internal use only.
