# ADR 0003 — Evidence versus reasoning

- **Status:** Accepted
- **Date:** 2025 (initial bootstrap)
- **Deciders:** Scout maintainers
- **Supersedes:** none

## Context

Scout uses AI agents to review code, and then publishes a verification report that a human uses to
authorize a change. Those two facts are in tension. A model can produce a fluent, specific, and
completely unfounded claim, and a report that renders that claim in the same visual and semantic
register as a passing test suite is worse than no report: it transfers the reader's trust from the
machine evidence to the prose.

The governing principle resolves the tension at the level of authority, not at the level of tone:
**AI investigates and recommends. Deterministic systems verify. Humans authorize.** Spec section 3.1
states it more plainly — AI is not a source of truth — and spec section 2 adds that Scout must never
silently transition from analysis to deployment authority.

The architectural problem is that "AI is not a source of truth" is a statement about intent. Intents
decay. If the separation of reasoning and evidence is a convention that reviewers are asked to
remember, it will hold until the first deadline and then be quietly widened by a well-meaning
change: a slightly higher confidence on a strongly-worded finding, a `PASS` recommendation with a
minor caveat, an AI conclusion rendered without its evidence line. Each individual step looks
reasonable; the end state is an AI-approved pull request.

So the decision had to be encoded as executable constraints, in the types that every producer must
construct and every consumer must read.

## Decision

**Reasoning and evidence are separated at the type level. An AI claim is inference, inference has a
confidence ceiling and a provenance classification, and no AI-only conclusion can be presented as a
verified fact or can by itself loosen the final recommendation.**

The mechanism has four layers, all in `scout_domain`.

### 1. Evidence carries its own strength

Every `EvidenceReference` declares an `EvidenceKind`, and every kind is classified in a fixed,
total mapping to `EvidenceStrength`. Strength is not a matter of opinion and is not settable by the
producer:

| Strength | Rank | Source | Kinds |
|---|---|---|---|
| `MACHINE` | 3 | Deterministic execution; cannot be argued with | `TOOL_RUN`, `TEST_RUN`, `GIT_DIFF`, `GITHUB_CHECK`, `ARTIFACT` |
| `INSPECTION` | 2 | Direct observation of a file or repository structure, pinned to path and line range | `FILE_RANGE`, `REPOSITORY_INSPECTION` |
| `AUTHORITATIVE` | 1 | A human answer or an approved engineering decision; outranks anything Scout inferred | `HUMAN_ANSWER`, `KNOWLEDGE_DOCUMENT` |
| `INFERENCE` | 0 | An AI claim; useful, never sufficient on its own | `AGENT_RUN` |

The mapping is total by construction: `EVIDENCE_STRENGTH` commits to a classification for every kind,
and the docstring names the test that enforces totality, `test_evidence_strength_is_total`. Adding an
evidence kind without classifying it is a failure, not an omission.

### 2. Confidence is capped by evidence strength

`Finding._enforce_evidence_discipline` clamps a finding's claimed confidence to the ceiling implied
by its strongest evidence:

| Strongest evidence | Ceiling |
|---|---|
| `MACHINE` | 1.00 |
| `INSPECTION` | 0.95 |
| `AUTHORITATIVE` | 0.95 |
| `INFERENCE` | **0.65** |
| none | 0.35, and `requires_human` is forced to `True` |

The clamp sets `confidence_capped=True`, so the reduction is visible in the report and on dashboards
rather than being silently absorbed. A model that returns `confidence=0.99` with no evidence does not
produce a confident finding; it produces a clamped finding that demands human attention.

### 3. There is an explicit "verified fact" predicate

`Finding.provenance` classifies every finding as `DETERMINISTIC`, `AI_ASSISTED`, `AI_ONLY`, or
`UNSUPPORTED`. `Finding.is_verified_fact` is true only when **all** of the following hold:

- `provenance is DETERMINISTIC` — the finding was produced by a tool, not by an agent;
- `is_machine_verified` — at least one cited reference has `MACHINE` strength;
- `not confidence_capped` — the claim did not have to be reduced to fit its evidence.

An AI-only finding fails the first condition structurally. There is no constructor argument, no flag,
and no prompt that can make `is_verified_fact` true for a conclusion resting only on other AI
reasoning. Consumers that want a weaker bar must ask for it explicitly and visibly —
`VerificationReport.evidence_backed_findings()` uses a documented rank ≥ 2 (INSPECTION) threshold,
which `INFERENCE` still cannot reach.

### 4. The recommendation is gated by recomputable facts

`VerificationReport._enforce_recommendation_gates` reads findings, criteria, and test outcomes and
refuses to let the recommendation exceed what they support:

- open questions bar `PASS` and `PASS_WITH_WARNINGS`;
- CRITICAL findings bar `PASS` and `PASS_WITH_WARNINGS`;
- HIGH findings bar `PASS`;
- failing tests bar `PASS` and `PASS_WITH_WARNINGS`;
- failed acceptance criteria bar `PASS`;
- a `BLOCKED` criterion requires an explicit open question explaining what is missing.

A `ReportSection` marked `UNVERIFIED` or `SKIPPED` must state why, and those reasons are surfaced in
`unverified_areas`. `CriterionVerification` may be `PASSED` only with both evidence and a recorded
observed result. `VerificationReportDraft` exists as a separate aggregation object specifically so a
stage cannot write into the final report and bypass the gates.

Together these make the report's honesty a property of the type rather than of the prompt, the
reviewer, or the release process.

## Consequences

**What becomes possible.**

- A reviewer can sort, filter, and render findings by strength and provenance, and can see at a glance
  which conclusions are machine-backed and which are suggestions.
- The AI layer can be improved independently of the evidence layer: a better model raises the quality
  of `AI_ASSISTED` findings, and cannot silently raise their authority.
- The report is a defensible artifact. Every claim either cites machine evidence, inspection, or a
  human decision, or it is labelled by its weakness.
- Cost and quality can be reasoned about separately, because the value of an agent run is bounded by
  the evidence it can cite.

**What becomes harder, and is accepted.**

- **Agents cannot self-certify.** A review agent that is certain still cannot mark its finding
  verified. This is the point of the decision, and it means genuinely correct AI-only findings will
  always render as suggestions.
- **Reports must surface unverified areas explicitly.** The report is not allowed to be silent about
  what it could not check; `unverified_areas` and the `UNVERIFIED`/`SKIPPED` section rules make
  incompleteness visible rather than implicit.
- **Confidence values are not directly comparable to a model's own estimate.** A capped finding
  records a clamped number plus a flag. Any consumer that displays confidence without the flag is
  misrepresenting it.
- **Some triage requires a human by design.** An `UNSUPPORTED` finding forces `requires_human`, so a
  noisy agent can generate human work. That is treated as an agent-quality problem to fix with
  prompting and evidence retrieval, not by lowering the floor.
- **The gates constrain the report generator.** It cannot produce a reassuring narrative when the
  facts do not support one, even when a human would have written something softer.

## Detection: how we would know this decision regressed

The decision is regressed by a change that lets an AI claim gain authority. Detectable regressions
have signatures, and each has a fast unit test that fails if the decision is reversed. The tests
below exist today in `tests/unit/domain/test_evidence_and_findings.py` and
`tests/unit/domain/test_report_and_models.py`; they need no database, no model provider, and no
network.

| Regression | Test that must fail |
|---|---|
| A new `EvidenceKind` is added and classified as `MACHINE` (or not classified) to route AI output past the ceiling | `test_every_evidence_kind_is_classified`, `test_every_strength_has_a_confidence_ceiling` |
| `CONFIDENCE_CEILING[INFERENCE]` is raised, or a finding's confidence is no longer clamped | `test_ai_only_finding_is_capped`, `test_inspection_backed_finding_has_a_higher_ceiling`, `test_machine_backed_finding_may_be_certain`, `test_confidence_below_the_ceiling_is_left_alone` |
| An AI-only finding becomes renderable as a fact | `test_agent_with_only_inference_is_ai_only`, `test_a_tool_finding_without_machine_evidence_is_not_a_verified_fact`, `test_agent_with_machine_evidence_is_ai_assisted` |
| A finding with no evidence stops demanding human attention | `test_unsupported_finding_is_capped_hard_and_flagged_for_humans`, `test_no_evidence_is_unsupported` |
| A recommendation gate is relaxed | `test_open_questions_block_pass`, `test_critical_findings_block_pass`, `test_high_findings_block_a_bare_pass`, `test_failing_tests_block_pass`, `test_failed_criterion_blocks_pass`, `test_blocked_criterion_requires_an_open_question` |
| A verification section can silently become a pass | `test_unverified_section_must_explain_itself`, `test_unverified_areas_are_surfaced` |
| A criterion can pass on assertion rather than observation | `test_a_criterion_cannot_pass_without_evidence`, `test_a_criterion_cannot_pass_without_an_observed_result` |
| Scout becomes able to authorize its own work | `test_scout_cannot_approve_its_own_work`, `test_approval_applies_only_to_its_commit` |
| A skipped or failed stage is recorded without an explanation | `test_skipped_stage_requires_a_reason`, `test_failed_stage_requires_an_error` |
| A record gains an update/delete path that would erase evidence of a reversal | **not yet written.** There is no `AuditRepository` yet; the test belongs with it and must assert that no update or delete operation is exposed for `audit_events`. |

If one of these tests is edited to accommodate a change, the correct response is a new ADR
superseding this one, not an amendment to the test. Note also that the docstring in
`models/evidence.py` refers to this totality test as `test_evidence_strength_is_total`; the test that
actually exists is `test_every_evidence_kind_is_classified`. The docstring name is stale — the
coverage is real, the label is not.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| **Trust the model's own confidence score** | Confidence is generated by the same process that produced the claim, so it carries no independent information about whether the claim is true. A fluent wrong answer is typically high-confidence. The score is retained only as a claim to be clamped, never as a measure of truth. |
| **Calibrate the cap by empirical accuracy per agent role** | Would make the authority of a finding depend on a tuning artifact that changes silently over time, and would still be an estimate rather than evidence. The cap is a property of the *evidence kind*, which is inspectable and stable. Calibration may inform prompt quality; it must not raise authority. |
| **Present AI findings as facts but flag them in the UI** | Relies on the reader noticing a UI affordance after the prose has already asserted the conclusion. The failure mode — a human authorizes on the basis of a rendered sentence — occurs before the flag is read. The constraint belongs in the type, not the presentation. |
| **Let a human reviewer promote an AI finding to verified during review** | A human *can* add an authoritative statement (through a `QuestionAnswer` or a recorded `EngineeringDecision`, which are `AUTHORITATIVE`), but promotion by a reviewer's assent to Scout's own claim is not independent evidence — it converts the reviewer's trust into a machine-grade fact. Human authority is expressed as an attribution, not as an upgrade. |
| **One blended confidence score across tool and AI findings** | Destroys the distinction the report exists to make. A single number invites comparison between a passing test and a plausible paragraph. Strength and provenance are reported alongside confidence so the two are never conflated. |
| **Enforce separation only in the report generator's prompt** | Prompt-level constraints are not constraints. They are unenforced, untestable, and the first thing to be dropped when a model output needs to fit a schema. The report generator is downstream of the gates and cannot bypass them. |
