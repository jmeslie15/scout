# ADR 0001 — Initial hardware target: Apple Silicon Mac Mini, M1, 16 GB RAM

- **Status:** Accepted
- **Date:** 2025 (initial bootstrap)
- **Deciders:** Scout maintainers
- **Supersedes:** none

## Context

Scout's first phase runs the entire platform on one always-on machine: the control plane
(`apps/api`, `apps/orchestrator`), the execution plane (`apps/worker`), PostgreSQL, Redis, and a
sandbox container that executes repository code. The machine also serves as the development host,
and it must stay available for webhook delivery around the clock.

At the time of the decision, two Apple Silicon Mac Minis were available: an **M1 with 16 GB RAM**
and an **M2 with 8 GB RAM**. Both are fanless desktop machines with soldered, non-upgradeable
unified memory: whatever the machine ships with is its ceiling for its entire service life.

The workload is specific, and it is worth stating precisely because it determines which resource
matters:

- **Orchestration is I/O-bound.** The orchestrator waits on webhook delivery, GitHub API calls,
  git clone/fetch, PostgreSQL round-trips, Redis stream reads, and model-provider latency. Almost
  none of that work is CPU-saturating.
- **Sandbox execution is bursty and memory-hungry.** `npm ci`, `pytest`, a Python dependency
  install, or a build are each short bursts of high memory demand (dependency graphs, test
  collection, intermediate artifacts) followed by longer periods of I/O wait.
- **Memory demand is concurrent and simultaneous.** Docker Desktop's Linux VM, PostgreSQL, Redis,
  the orchestrator, the worker, and the sandbox container are all resident at the same time. Memory
  is consumed in gigabytes continuously; CPU is consumed in short spikes.
- **Sandbox concurrency is already limited by design.** `SCOUT_MAX_CONCURRENT_SANDBOXES=1`,
  `SCOUT_SANDBOX_CPUS=1.0`, and `SCOUT_SANDBOX_MEMORY_MB=2048` are the configured defaults, and
  `ResourceLimits` (1 CPU, 2 GiB, 256 PIDs) is the per-sandbox contract. Additional CPU cores cannot
  be used for additional concurrent sandboxes until memory allows it.

## Decision

**The initial deployment target is an Apple Silicon Mac Mini, M1, 16 GB RAM.**

Chosen over an M2 Mac Mini with 8 GB RAM. Memory capacity dominates CPU generation for this
workload, and 16 GB is a hard requirement rather than a preference.

### Why 16 GB is a hard requirement

An approximate resident-memory budget on a 16 GB host, with the platform idle but PostgreSQL, Redis,
and one sandbox held warm:

| Consumer | Approximate resident footprint | Notes |
|---|---|---|
| macOS + background services | ~3–4 GB | Not compressible by Scout. |
| Docker Desktop Linux VM | ~2–3 GB | Fixed by Docker Desktop's allocation; the sandbox runs inside it. |
| PostgreSQL | ~1 GB | `shared_buffers`, per-connection work memory, WAL, and OS page cache pressure. |
| Redis | ~0.5 GB | Job stream, consumer-group pending entries, dedup keys, knowledge cache. |
| `apps/api` + `apps/orchestrator` | ~0.4 GB | Mostly idle; event loop plus request buffers. |
| `apps/worker` | ~0.5–1 GB | Git objects, diff parsing, selectively retrieved file contents, agent context. |
| Sandbox container | up to 2 GB | `SCOUT_SANDBOX_MEMORY_MB=2048`; a cap, not a reservation — but a `pytest` run will use most of it. |
| **Headroom for `npm ci` / `pytest` spikes and page cache** | **~3–5 GB** | This is the margin that keeps a burst from becoming an eviction. |

The budget does not fit in 8 GB. Removing the headroom row gives roughly 8–9 GB of steady-state
demand before any burst, on a machine where the OS and the Docker VM also need room to cache file
I/O — which is precisely what `npm ci` and `pytest` are doing. On an 8 GB machine the steady state
already touches the ceiling, so the first `npm ci` pushes the system into sustained swap.

The failure mode is not merely slowness. Under memory pressure macOS compresses and then terminates
resident processes, and a container that exceeds its VM allocation is killed by the Docker VM. The
largest, longest-lived, most expensive-to-lose resident process is PostgreSQL. Losing it mid-job
corrupts nothing by design — jobs are resumable and leases are persisted (Rule 5) — but it turns a
verification into a failed job, an operator intervention, and a noisy audit trail. Swap thrashing
also destroys the one thing Scout's pipeline depends on for its timeouts: predictable wall-clock
behaviour. `SCOUT_SANDBOX_COMMAND_TIMEOUT_SECONDS=900` and `SCOUT_MAX_JOB_RUNTIME_SECONDS=1800` are
meaningless if the host is paging.

### Why M2's CPU advantage does not compensate

The M2 is roughly 10–20% faster than the M1 in single-core work. That advantage is real and it is
worth less than the 8 GB:

1. **The pipeline is not CPU-bound.** Orchestration, webhook handling, GitHub calls, scheduling, and
   state persistence are I/O-bound. A faster core shortens those phases by approximately nothing.
2. **The CPU-bound phase is capped.** The sandbox is limited to 1 CPU and one concurrent sandbox.
   Even a 20% faster core shortens a test run's compute portion by 20% of a fraction of the run.
3. **The bottleneck the CPU cannot fix is memory.** With `SCOUT_MAX_CONCURRENT_SANDBOXES=1`, the
   second sandbox cannot start until the first finishes, and the first cannot be admitted at all if
   there is no memory for it. Extra CPU speed cannot raise concurrency; extra memory can.
4. **CPU is not upgradeable either — but it is the one thing Scout measurably does not need.**
   Choosing the faster CPU at the cost of the memory ceiling trades a resource Scout has slack in
   for one it does not.

### Consequences

**Configuration defaults tied to this decision.**

- `SCOUT_MAX_CONCURRENT_SANDBOXES=1` — one sandbox at a time, sized to the 2 GB per-sandbox cap plus
  the rest of the steady-state footprint.
- `SCOUT_MAX_CONCURRENT_JOBS=2` — jobs may queue and interleave, but only one reaches sandbox
  execution at a time.
- `SCOUT_SANDBOX_MEMORY_MB=2048`, `SCOUT_SANDBOX_CPUS=1.0`, `SCOUT_SANDBOX_PIDS_LIMIT=256` — the
  per-sandbox ceiling that makes the budget above predictable. These are configuration, not
  constants: `ResourceLimits` takes them as validated fields (`memory_mb` up to 65 536, `cpus` up to
  64) so they can rise on a larger host.
- `SCOUT_MAX_REPO_SIZE_MB=2048` — a clone large enough to threaten the host is refused rather than
  attempted.
- `SCOUT_SANDBOX_BACKEND=local` for development (no Docker VM overhead) and `docker` for real use
  (isolation), per `.env.example`.

**Portability is a design requirement, not a hope.** Nothing in the application may assume a single
machine, macOS, or a local filesystem. The evolution path is:

1. **Single machine** (this decision): all processes, PostgreSQL, Redis, and the sandbox on one Mac
   Mini.
2. **Multiple workers**: additional `apps/worker` processes (or a second machine) join the same
   Redis consumer group and PostgreSQL schema, raising sandbox concurrency beyond 1. The lease
   mechanism (`lease_owner`, `lease_expires_at`) already makes a single job single-owner, so this
   step requires configuration, not code.
3. **Linux server + object storage**: PostgreSQL, Redis, and workers move to a Linux host; the
   sandbox runs natively as a container without Docker Desktop's VM; the content-addressed artifact
   store moves from `SCOUT_ARTIFACT_ROOT` to an object store behind the same `Artifact.storage_key`
   locator. Linux is the better home for container isolation, and memory stops being soldered.
4. **Container orchestration**: workers and sandboxes scheduled by an orchestrator, with the same
   queue, lease, and artifact contracts.

Each step changes configuration, deployment topology, and backend implementations — not the domain
model, the state machine, or the evidence contract.

**Accepted costs.**

- One machine is a single point of failure for the pilot. Jobs are resumable, the queue is durable,
  and state is in PostgreSQL, so the loss is availability rather than correctness.
- Sandbox throughput is one at a time. During the pilot this is a deliberate throttle as much as a
  hardware limit: it keeps resource behaviour predictable while the pipeline is being proven.
- The machine cannot be upgraded in place. A memory increase means a machine replacement, which is
  why the portability requirement above is stated as a design constraint rather than deferred.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **M2 Mac Mini, 8 GB** | The decision's counterfactual. ~10–20% faster single-core, but the 8 GB ceiling is soldered, so the constraint can never be relieved. Steady-state demand already approaches the ceiling; `npm ci`/`pytest` bursts cause swap thrashing, and memory pressure risks terminating PostgreSQL mid-job. Memory capacity dominates CPU generation here. |
| **M1 Mac Mini, 8 GB** | Same unupgradeable ceiling with no compensating advantage over the 16 GB M1. Strictly worse for the same class of machine. |
| **M2 Mac Mini, 16 GB** | Capability-wise preferable — it would not violate the memory requirement, and it is faster. Rejected for the initial bootstrap machine on cost and availability relative to the M1 16 GB, not on suitability. If a 16 GB M2 is available at the same effective cost, it is a fine substitute; the decision that matters is 16 GB, not the CPU generation. |
| **Apple Silicon laptop as the always-on host** | Thermal throttling under sustained bursts, sleep/lid behaviour, and battery health make a laptop a poor always-on server. It also couples the platform's availability to a developer's machine. |
| **x86 Linux mini-PC / NUC** | Genuinely attractive: native containers, no Docker Desktop VM, cheaper memory upgrades. Rejected for phase 1 because the first phase needs one machine that is also the team's development host, and because a Linux host is the *destination* of evolution phase 3, not a reason to skip the phase-1 portability work. Revisit at phase 3. |
| **Cloud VM (≈4 vCPU / 16 GB)** | Rejected for phase 1: recurring cost for an always-on box, clone egress and webhook round-trips, repository contents leaving the local network during the pilot, and a slower edit-run loop. Revisit when throughput, not iteration speed, is the constraint. |
| **Buy 8 GB now, add memory later** | Impossible on Apple Silicon. Unified memory is soldered and not upgradeable, so this is not a deferral — it is a permanent constraint. This is the single strongest argument for the 16 GB machine. |
