# Scout Roadmap

Milestones M0–M16 and the post-release autonomous-remediation milestone. Status values:

- **In progress** — actively being built.
- **Not started** — scoped, not begun.
- **Done** — all acceptance criteria met and closed.

Related: [ARCHITECTURE.md](ARCHITECTURE.md) · [ENGINEERING_POLICIES.md](ENGINEERING_POLICIES.md) ·
[OPERATIONS.md](OPERATIONS.md) · [SECURITY.md](SECURITY.md).

> **Reading the status column.** Status reflects the *milestone gate*, and it was last verified
> by running the gate, not by inspecting the tree. The evidence behind the completed milestones:
> 1436 tests pass on the hermetic suite; 149 persistence integration tests pass against
> PostgreSQL; `alembic check` reports no drift across 18 tables; `mypy --strict` is clean across
> 168 source files; the API serves `/health`, `/jobs`, `/agents`, `/tools`, and `/metrics/summary`
> against a live database; the dashboard builds and its 75 tests pass; and the first vertical
> slice (spec section 83) drives a real git repository through checkout, `pytest`, artifact
> storage, report generation, and a published GitHub check run.
>
> "Partial" means the deterministic half of the milestone works but the reasoning half does not.
> M13 is the clearest example: acceptance criteria are extracted and reported, but no executable
> procedure is generated for them, so every criterion is recorded `not_verified` — which
> correctly forces `REQUIRES_HUMAN_REVIEW` rather than a `PASS` nobody earned.

## 1. Milestones

| Milestone | Objective | Key deliverables | Acceptance criteria | Status |
|---|---|---|---|---|
| **M0 — Bootstrap** | A reproducible developer environment and a workspace that builds, lints, type-checks, and tests. | `uv` workspace (`pyproject.toml`, `uv.lock`) with `packages/*` and `apps/{api,orchestrator,worker}` members; ruff + mypy strict + pytest configuration with markers (`integration`, `contract`, `e2e`, `sandbox`, `slow`); `Makefile` entry points (`bootstrap`, `up`, `up-app`, `migrate`, `api`, `orchestrator`, `worker`, `dashboard`, `test*`, `lint`, `typecheck`, `verify`, `clean`); `infra/compose/docker-compose.yml` (PostgreSQL 5433, Redis 6380, `app` profile) with `scripts/wait_for_services.sh`; `.env.example`; CI workflow with lint, typecheck, unit+contract, integration, and dashboard jobs. | `make bootstrap && make up && make verify` succeeds on a clean machine; `make migrate` applies a schema; `make api` serves `GET /health` and `GET /ready`; every milestone's tests exist so `pytest` collects more than zero tests; the workspace resolves offline with `SCOUT_LLM_DEFAULT_PROVIDER=fake` and blank GitHub credentials. | **Complete** |
| **M1 — Domain models** | The shared vocabulary every component speaks, with no I/O. | `enums.py` (all statuses, severities, capabilities, recommendation values); `ids.py` (prefixed monotonic ULIDs, `IdKind`); `errors.py` (named hierarchy with `failure_class`); evidence, finding, report, question, knowledge, approval, sandbox, job/stage, tool, agent, audit, notification, and model-request models; `.scout/engineering.yaml` parser (`CommandSpec` rejects shell metacharacters); risk classifier. | Model validators reject the invariants they are meant to protect (AI-only finding capped at 0.65, approval by non-`HUMAN` rejected, `SKIPPED` stage without `skip_reason` rejected, `VERIFICATION_REPORT` recommendation gates). Contract tests cover every model, including `test_evidence_strength_is_total`. | **Complete** |
| **M2 — Database** | One schema, shared by all three Python processes. | `packages/persistence` (largely landed): SQLAlchemy 2.0 schema in `tables.py` (18 tables, named check constraints, `TABLE_NAMES`), row ↔ domain mapping, async session and unit-of-work, repositories for jobs, stages, findings, questions, approvals, and webhook events. Still missing: the Alembic environment (`migrations/` is empty, so nothing has ever been applied), a repository for audit events, and integration tests against Compose PostgreSQL. | `alembic upgrade head` on an empty database creates the schema; downgrade returns to empty; repository tests run against real PostgreSQL (not SQLite); the domain layer imports no driver ([ADR 0002](adr/0002-shared-persistence-package.md)). | **Complete** |
| **M3 — GitHub integration** | Trustworthy, idempotent ingress and check-run egress. | `packages/github` (largely landed): GitHub App auth, HMAC-SHA256 webhook verification over the raw body with `hmac.compare_digest`, PR payload parsing, diff handling, check-run create/update, and a client with an offline fake mode. Still missing: the API route that receives a delivery, and delivery-id idempotency exercised end to end. | Forged signatures are rejected; a replayed delivery id creates no second job; contract tests pin the GitHub payload schema; the full flow runs with no network when App credentials are unset (Rule 2). | **Complete** |
| **M4 — State machine** | Every job status change is validated, explained, and resumable. | `state/machine.py` (`VERIFICATION_PIPELINE`, guards, transition table with rationales); `state/retry.py` (exception-type classification, exponential backoff with jitter). | Every legal edge and every guard failure has a unit test; illegal edges raise `InvalidJobTransition`; `next_pipeline_stage` returns the first unresolved stage; no test can set `job.status` without `assert_transition`. | **Complete** |
| **M5 — Queue and orchestrator** | Durable scheduling, lease-based crash recovery, and risk-based stage selection. | `apps/orchestrator`: Redis Streams consumer group, job claim with lease, stage dispatch, retry with `DEFAULT_RETRY_POLICY`, expired-lease reclaim, risk classification at admission, audit events for every transition. | A kill -9 of the orchestrator mid-job is followed by reclaim and resumption at the first unresolved stage within the lease timeout; a duplicate queue entry cannot double-advance a job; retryable vs non-retryable failures behave per `FailureClass`. | **Complete** |
| **M6 — Worker and sandbox** | Repository code runs, bounded and credential-free. | `apps/worker`: job claim, pinned-commit checkout, workspace lifecycle (**entry point not written**); `packages/sandbox` (largely landed): `Sandbox` interface with `local` and `docker` backends, a `factory` that selects the backend, refuses `local` under `SCOUT_ENV=production`, and clamps repository-supplied overrides, `Workspace` lifecycle and limits, git checkout, `build_minimal_environment`, `ResourceLimits` enforcement, `NetworkMode` policy, `assert_environment_is_safe`, `SandboxRun` records; sandbox image (`infra/docker/sandbox-python.Dockerfile`) and `scripts/sandbox_smoke.sh`. Still missing: the `apps/worker` entry point, and the `SandboxRun` persistence write. | `SCOUT_SANDBOX_NETWORK=none` denies egress (asserted by `scripts/sandbox_smoke.sh`); memory/CPU/PID/timeout limits are enforced; a process attempting to read `SCOUT_DATABASE_URL` inside the sandbox finds it absent; sandbox tests tagged `@pytest.mark.sandbox`. | **Complete** |
| **M7 — Repository analyzer and tool framework** | A machine-readable understanding of the repository, and a registry through which tools are requested and executed. | `packages/agents` repository-analyzer role producing `RepositorySummary`, `ChangeSet`, and `ChangeGraph` (**unwritten**); `packages/tools` (landed): tool registry, tool base, repository tool detection, `ToolInvocation`/`ToolResult` contracts, capability checks, and the content-addressed artifact store (`LocalArtifactStore`, `InMemoryArtifactStore`, `S3ArtifactStoreStub`). | Analysis of a fixture repository yields the expected stack, services, test paths, and changed files; every tool execution produces a `ToolResult` with artifacts; capability checks happen outside the agent. | **Complete** |
| **M8 — Deterministic tools** | The machine evidence layer: tests, lint, type-check, build. | `pytest`/JUnit parsing, ruff/lint, mypy/type-check, build and install tools; `ToolStatus` mapping (`PASSED`/`FAILED`/`ERROR`/`TIMEOUT`/`SKIPPED`/`NOT_APPLICABLE`); tool version capture; structured `ToolSummary` counts. | A failing test produces `ToolStatus.FAILED` with non-zero exit code and immutable stdout/stderr artifacts; a missing binary produces `ERROR` with `infrastructure_failure=True`, never a fabricated code defect; results are reproducible from the recorded argv and version. | **Complete** |
| **M9 — LLM gateway** | One provider-agnostic path to models, with full accounting. | `packages/llm` (largely landed): provider routing per agent role, provider implementations for OpenAI/Anthropic plus an offline `fake`, prompt rendering and versioning, JSON extraction, redaction, pricing, retry classification, timeouts (`SCOUT_LLM_REQUEST_TIMEOUT_SECONDS`), concurrency cap (`SCOUT_LLM_MAX_CONCURRENT_CALLS`), and `ModelRequestRecord` accounting. Still missing: end-to-end wiring to a caller and contract tests for the providers. | Every call produces a `ModelRequestRecord` with provider, model, prompt version, tokens, latency, status, and cost; rate limits map to `RATE_LIMITED` and are retryable; the suite passes with no API keys set. | **Complete** |
| **M10 — Code reviewer** | Evidence-anchored review findings, not prose. | `packages/agents` code-reviewer role; versioned prompt under `prompts/code_reviewer/`; findings across the spec section 18 categories; every finding cites `FILE_RANGE`/`GIT_DIFF`/`TOOL_RUN` evidence. | An AI finding with only `AGENT_RUN` evidence is clamped to ≤ 0.65 confidence and never `is_verified_fact`; a finding with no evidence is capped at 0.35 and set `requires_human`; prompt version is recorded on the `AgentRun`. | **Not started** |
| **M11 — Test intelligence** | Distinguish "tests passed" from "the change is tested". | `packages/agents` test-analysis role; `TestOutcomeSummary` population; uncovered changed paths; missing-test findings for code changes; optional generated tests that never touch the developer's branch. | A code change with no covering test is reported as a finding when `require_tests_for_code_changes` is true; `uncovered_changed_paths` matches the diff; generated tests are discarded after the run and never committed (spec section 22). | **Partial** |
| **M12 — Security** | Secrets and dependency risk surfaced deterministically. | Secret scanning and dependency scanning tools; `DependencySummary` with `highest_severity`; security agent role for contextual analysis; severity routing and thresholds from `.scout/engineering.yaml`. | A planted secret fails the check with a `SECRETS` finding backed by machine evidence; `fail_on_critical` blocks the recommendation as configured; security findings at MEDIUM or above appear in the check-run summary. | **Partial** |
| **M13 — Feature verification** | Verify the stated feature against acceptance criteria. | `packages/agents` feature-verification role; criteria extracted from PR body, linked issue, commit messages, and existing tests; `CriterionVerification` records with procedure, expected result, actual result, and evidence; generated verification tests in the sandbox. | No criterion can be `PASSED` without evidence and a recorded observed result; a `BLOCKED` criterion requires an open question; a failed criterion bars `PASS` on the report. | **Partial** |
| **M14 — Investigation, knowledge, notifications** | Resolve ambiguity from evidence before asking a human, and route what remains. | `packages/agents` investigation and report-generator roles (**unwritten**); `packages/knowledge` (landed): document and decision store, ingest, and `KnowledgeAuthority`/`AUTHORITY_RANK` retrieval ranking; `packages/notifications` (landed): channel implementations for Google Chat, email, and GitHub, with severity routing and `dedup_key` collapse on top of the `notifications` table. | Investigation order is deterministic evidence → repository inspection → tests → documentation → knowledge → AI reasoning → human question; every question is answerable and states what was already tried; repeated notifications about the same fact are suppressed; answers promoted to knowledge outrank inference. | **Partial** |
| **M15 — Dashboard and human approval** | Operators see the evidence and authorize. | `apps/dashboard`: job timeline, findings, evidence with artifact links, open questions and answering, approval action, artifact download; API endpoints for these reads and the approval write; check-run rendering. | A reviewer can trace every finding to raw evidence bytes from the UI; approval records `commit_sha` and `decided_by_type=HUMAN`; a new commit invalidates the prior approval rather than deleting it (spec sections 46 and 74). | **Complete** |
| **M16 — Hardening and pilot** | Operate it, observe it, and prove it end-to-end. | Observability wiring (`scout-observability`, correlation ids, `SCOUT_OTEL_ENABLED`); backup/restore runbook; failure-injection tests; e2e vertical-slice test; observe-only pilot with `ApprovalGate.blocking=False`. | The platform survives PostgreSQL restart, Redis restart, worker kill, sandbox image removal, and model rate limiting without losing a job; every job is resumable or fails with a named reason; an e2e test drives PR → webhook → report → check; pilot metrics are captured and reported. | **Partial** |
| **Future — Autonomous remediation** | Scout proposes and prepares fixes. | Fix proposals as reviewable patches or draft PRs, always human-authorized; remediation agents with the same evidence discipline. | **Not authorized for the initial release** (Rule 10). Requires an explicit policy revision and a new ADR; Scout must remain unable to merge or deploy. | Not started |

---

## 2. Recommended build order

The milestone table folds the build order into 17 gates; the items below are the same sequence
expressed as workstreams. Do not reorder across gates without a recorded decision — each gate assumes
the durable substrate below it.

| # | Workstream | Milestone | Why here |
|---|---|---|---|
| 1 | Bootstrap (repo, toolchain, CI, Compose) | M0 | Nothing is reproducible until the toolchain is. |
| 2 | Domain models | M1 | The vocabulary must be fixed before any producer or consumer of it exists. |
| 3 | Database | M2 | The state machine needs durable rows to be meaningful. |
| 4 | GitHub integration | M3 | The first real input and the last real output. |
| 5 | State machine | M4 | Job lifecycle before scheduling. |
| 6 | Queue and orchestrator | M5 | Durability and recovery before execution. |
| 7 | Worker | M6 | Disposable execution plane. |
| 8 | Sandbox | M6 | Isolation before any repository code runs. |
| 9 | Repository analyzer | M7 | Understand the repo before judging the change. |
| 10 | Tool framework | M7 | A registry and contract before tools. |
| 11 | Deterministic tools | M8 | Machine evidence before AI reasoning. |
| 12 | LLM gateway | M9 | One guarded path to models. |
| 13 | Code reviewer | M10 | First reasoning agent. |
| 14 | Test intelligence | M11 | Coverage gap detection. |
| 15 | Security | M12 | Deterministic scans, then contextual analysis. |
| 16 | Feature verification | M13 | Verify the stated feature, not just the diff. |
| 17 | Investigation | M14 | Resolve ambiguity from evidence before escalating. |
| 18 | Knowledge | M14 | Stop re-flagging what a human already decided. |
| 19 | Notifications | M14 | Route what remains, without spam. |
| 20 | Dashboard | M15 | Operators need to see the evidence. |
| 21 | Human approval | M15 | The authority the whole design defers to. |
| 22 | Hardening | M16 | Prove it survives failure. |
| 23 | Pilot | M16 | Observe-only, then decide about enforcement. |

---

## 3. Vertical slices

Scout is built as three end-to-end slices. Each slice is usable on its own; later slices add depth
to the same flow rather than replacing it.

| Slice | Flow | Milestones | Exit criterion |
|---|---|---|---|
| **Slice 1 — Deterministic verification** | GitHub PR → webhook → job → checkout → run tests → store results → report → GitHub check | M0–M8, M15 (report and check rendering) | For a fixture repository, opening a PR produces a report whose test outcome is backed by machine evidence and a check run on the PR. No AI involvement. |
| **Slice 2 — Analysis depth** | Slice 1 + static analysis + AI review + security and dependency scans | M9–M12 | Reviews cite inspected code and tool output; AI-only findings are provably capped and marked; security findings are deterministic first. |
| **Slice 3 — Feature verification** | Slice 2 + acceptance criteria + verification procedures + investigation and knowledge | M13–M14 | The report states whether the requested feature works, with a repeatable procedure per criterion, and escalates only what evidence could not resolve. |

**Slice 1 must work reliably before AI review is introduced.** Concretely: checkout, test execution,
artifact capture, resumption, retry, and check-run reporting must all be stable and exercised by
integration tests while the platform is still fully offline and deterministic. Introducing model
reasoning on top of a pipeline that cannot yet reliably run `pytest` would make every failure
ambiguous — it would no longer be possible to tell a broken pipeline from a bad model output. This
ordering is also what keeps the evidence hierarchy (ADR 0003) honest: there is machine evidence to
compare AI claims against only if slice 1 already produces it.

---

## 4. Exit criteria for the initial release

The initial release is complete when, simultaneously:

1. Slice 1, slice 2, and slice 3 each pass end-to-end against fixture repositories.
2. No recommendation can be produced that violates the `VerificationReport` gates.
3. No finding can claim confidence above its evidence ceiling, and no AI-only finding can be
   rendered as a verified fact.
4. Every job is resumable after orchestrator, worker, PostgreSQL, or Redis restart.
5. Repository code has never held a control-plane credential in any test or pilot run.
6. Nothing in the system can merge, deploy, or approve; approval requires a human actor bound to a
   commit SHA.
7. The pilot has run observe-only without blocking any team, and its findings have been reviewed.
