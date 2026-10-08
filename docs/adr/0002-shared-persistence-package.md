# ADR 0002 — A shared `packages/persistence` workspace member

- **Status:** Accepted
- **Date:** 2025 (initial bootstrap)
- **Deciders:** Scout maintainers
- **Supersedes:** none

## Context

Scout's original package layout assumed persistence would live wherever it was needed: `apps/api`
would own its database access, `apps/orchestrator` would own its own, and `apps/worker` would persist
its results. That assumption does not survive contact with the state machine.

Three processes read and write the same rows, and they must agree on their shape exactly:

| Process | What it does with persistent state |
|---|---|
| `apps/api` | Creates `VerificationJob` on webhook receipt; reads jobs, findings, evidence, questions; writes `QuestionAnswer` and `Approval`; writes audit events. |
| `apps/orchestrator` | Reads and transitions job status; appends `JobStage` rows; claims and renews leases; records retries and audit events; writes the `VerificationReport`. |
| `apps/worker` | Reads the job and its pinned commit; writes `ToolResult`, `AgentRun`, `SandboxRun`, `Artifact` locators, `Finding`, and `EvidenceReference` rows. |

The domain models (`scout_domain.models.*`) are the vocabulary, but they are deliberately pure: the
`scout-domain` package declares only `pydantic`, `pydantic-settings`, and `pyyaml`, and its
description is explicit — *pure domain layer … No I/O*. It has no database driver and must not
acquire one. The question this ADR answers is where the *mapping* from those domain models to tables
lives, given that three processes need it and the pure layer cannot hold it.

Two policies constrain the answer:

- **Rule 14 — avoid premature microservices.** Splitting persistence into its own service would add a
  network hop, a deployment unit, a failure mode, and a version-skew problem to solve a problem that
  is not a scaling problem.
- **Rule 1 — do not introduce infrastructure before it is necessary.** Any new package must be
  justified by an existing need, not a predicted one.

## Decision

**Add `packages/persistence` (distribution name `scout-persistence`) as a workspace member holding
the SQLAlchemy 2.0 schema, the repositories, the unit-of-work/session handling, and the Alembic
environment. `apps/api`, `apps/orchestrator`, and `apps/worker` all depend on it.**

Concretely, as it exists in the workspace:

- `pyproject.toml` declares `scout-persistence` and depends on `scout-domain`, `sqlalchemy[asyncio]`,
  `asyncpg`, and `alembic`.
- `[tool.uv.workspace] members = ["packages/*", "apps/api", "apps/orchestrator", "apps/worker"]`
  includes it via `packages/*`, and the workspace root lists `scout-persistence` among its
  dependencies.
- All three application `pyproject.toml` files declare `scout-persistence` and map it with
  `scout-persistence = { workspace = true }`.

This is a **shared library, not a service**. There is no persistence process, no persistence HTTP
surface, and no network boundary: each process imports the same code and opens its own connection
pool to the same PostgreSQL instance at `SCOUT_DATABASE_URL`.

### Justification against Rule 14 (avoid premature microservices)

The rule forbids splitting a system into services before there is a reason to. A shared library is
the opposite move: it is what keeps three processes from becoming three divergent schemas. The
processes still communicate through PostgreSQL and Redis rather than through service-to-service RPC,
and the deployment topology is unchanged — `scout-persistence` is not deployed at all. If Scout ever
genuinely needed independent persistence scaling, that decision would be a separate ADR with
different evidence, and this ADR would be superseded rather than edited.

### Justification against Rule 1 (no infrastructure before necessary)

This is not anticipatory infrastructure; three processes already need one schema in the initial
vertical slice. The need is present at the moment the package is introduced:

- The state machine's resumability guarantee (Rule 5, spec section 50) requires that a `JobStage`
  written by a worker is readable by the orchestrator with identical semantics.
- The lease mechanism (`lease_owner`, `lease_expires_at`) is a row that the API and the orchestrator
  both read.
- The report is assembled by the orchestrator from `Finding`, `EvidenceReference`, and `ToolResult`
  rows written by the worker, and is then read by the API to render the dashboard and the GitHub
  check.

Three writers, one schema, one migration history. One package is the minimum number of packages that
expresses that.

## Consequences

- **One schema, one migration history.** A single Alembic environment means there is exactly one
  answer to "what does the database look like right now". `alembic upgrade head` is meaningful for
  the whole platform, not per process.
- **One place to enforce append-only constraints.** Rule 6 (privileged actions are audited) and
  Rule 15 (raw evidence is preserved) are properties of the repository layer. With one layer, the
  absence of an update/delete path for `AuditEvent` and the immutability of artifact rows are
  enforced once.
- **One place to keep the domain pure.** `scout-persistence` depends on `scout-domain`, never the
  reverse. The import direction is checked by the package boundary: `scout-domain` does not declare
  SQLAlchemy, so an accidental import fails at lint and build time rather than at runtime.
- **Schema changes are cross-cutting.** A column change affects three processes simultaneously. This
  is a real cost, and it is the correct one: the alternative is three codebases discovering the same
  migration independently.
- **The schema has landed; the repositories and Alembic environment have not.** The declared schema
  is in `packages/persistence/scout_persistence/tables.py` (17 tables, named check constraints, an
  explicit `TABLE_NAMES` tuple for a contract test). `scout_persistence/__init__.py` is still empty,
  so there are no repositories, no session/unit-of-work, and no mapping layer yet — and
  `migrations/` contains only an empty `versions/`.
- **Alembic environment location is still not settled in the tree.** `packages/persistence`'s
  description claims the Alembic migration environment, while the README layout lists a top-level
  `migrations/` directory. The `Makefile` currently resolves the ambiguity in favour of the root
  (`uv run alembic -c migrations/alembic.ini …`), and CI guards on the existence of that file — but
  neither `alembic.ini` nor `env.py` exists in either location. The decision recorded here is *where
  the schema and repositories live*; whichever location the environment lands in, there will be
  exactly one.
- **Connection pools multiply with processes.** PostgreSQL sees one pool per process rather than one
  shared pool. On the initial single-machine target this is bounded and acceptable (see
  [ADR 0001](0001-initial-hardware-target.md)); connection limits are an operational tuning concern
  documented in [OPERATIONS.md](../OPERATIONS.md).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Put SQLAlchemy models inside `packages/domain`** | It would make the pure domain layer depend on a database driver, contradicting the `scout-domain` description ("No I/O") and its dependency list. The domain layer's value is that it can be imported and tested with no infrastructure at all: `EvidenceReference`, `Finding`, and the state machine are unit-testable with no PostgreSQL running. A database dependency in that layer destroys the fastest and most valuable test tier, and it is exactly what makes the domain reusable across processes, migrations, and a future storage backend change. |
| **Duplicate models per app** | Guaranteed drift. Three ORM mappings of `VerificationJob` means three chances to forget a column, three definitions of "resolved stage", and a class of bug — the orchestrator and the worker disagreeing about what a stage outcome means — that is invisible until a job is silently mis-resumed. Migrations would also have to be written three times or coordinated out-of-band. |
| **A persistence service with an RPC/HTTP surface** | A deliberate microservice, rejected under Rule 14. It adds a deployment unit, a network failure mode, request-serialisation overhead on hot paths, and version skew between services and schema, in exchange for isolation Scout does not need at this scale. |
| **Repositories in each app, models in one shared package** | Splits the schema from its access patterns, so the two can drift: a repository in `apps/worker` could issue a query that contradicts the model's validator-enforced invariants, and the invariants would then be enforced in three places or nowhere. Keeping schema and repositories together makes the invariants enforceable at one boundary. |
| **Use the ORM entities as the domain models (no mapping layer)** | Would leak persistence concerns into the domain and make the wire format of every domain object a function of its table shape. `ScoutModel` uses `extra="forbid"` deliberately, and agent output and webhook payloads are validated against these models — a model whose fields are chosen by a migration is the wrong contract for untrusted input. |
