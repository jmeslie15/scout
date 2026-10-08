# Architecture Decision Records

An ADR records a decision that was expensive to make and would be expensive to reverse: the
context that forced it, the decision itself, and its consequences. Records are numbered
sequentially, named `NNNN-short-title.md`, and are **immutable once accepted** — a record is never
edited to reflect a later change of mind. When a decision is revisited, a new ADR is written and the
superseded record's status becomes `Superseded by ADR NNNN`, with its original text left intact so
that the reasoning that was actually available at the time remains legible. A record may also be
`Proposed`, `Accepted`, `Rejected`, or `Deprecated`.

Each ADR uses the same sections: **Context**, **Decision**, **Consequences**, **Alternatives
considered**. Keep them short — an ADR that needs a page of preamble is usually two decisions.

Scout-specific conventions worth knowing when reading these records:

- Rule numbers (Rule 1–Rule 15) refer to [ENGINEERING_POLICIES.md](../ENGINEERING_POLICIES.md).
- "spec section N" refers to the numbered sections of the Scout product specification quoted in
  domain-model docstrings.
- A decision that adds a package, a service, or a datastore must argue explicitly against Rule 1
  (no infrastructure before necessary) and Rule 14 (avoid premature microservices).

## Index

| ADR | Title | Status | Summary |
|---|---|---|---|
| [0001](0001-initial-hardware-target.md) | Initial hardware target | Accepted | The initial deployment target is an Apple Silicon Mac Mini, M1, 16 GB RAM. Memory capacity dominates CPU generation for Scout's workload, and 8 GB is an unupgradeable ceiling. |
| [0002](0002-shared-persistence-package.md) | Shared persistence package | Accepted | `packages/persistence` holds the SQLAlchemy schema, repositories, and Alembic environment, shared by `apps/api`, `apps/orchestrator`, and `apps/worker` — one schema for three processes, not a service. |
| [0003](0003-evidence-versus-reasoning.md) | Evidence versus reasoning | Accepted | Reasoning and evidence are separated: AI output is inference with a confidence ceiling and can never be a verified fact; the recommendation is gated by machine-checkable facts. |
