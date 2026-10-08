/**
 * Presentation mappings for Scout's vocabulary.
 *
 * Every function here is total: an unknown value from a newer backend falls back
 * to a neutral tone rather than rendering blank. Keeping the mapping in one
 * module (instead of colour literals scattered across components) is what makes
 * the tests in `src/lib/badges.test.ts` meaningful.
 *
 * COLOUR MAPPING FOR `JobStatus`
 * ------------------------------
 * The job status is the dashboard's most-read value, so the palette separates
 * four semantically different situations rather than just "good/bad":
 *
 *   tone        statuses                                        meaning
 *   ----------  ----------------------------------------------  --------------------------------
 *   `active`    RECEIVED, QUEUED, PREPARING, ANALYZING,         Scout is working; nothing for a
 *               REVIEWING, SECURITY_SCANNING, TESTING,          human to do yet. Rendered with a
 *               FEATURE_VERIFYING, INVESTIGATING                pulsing dot because it is a
 *                                                               "this will change" state.
 *   `review`    READY_FOR_REVIEW                                Verification finished cleanly and
 *                                                               the job is on a human's desk.
 *   `human`     WAITING_FOR_HUMAN                               Blocked on a human answer, not a
 *                                                               decision. Amber, distinct from
 *                                                               `review` so operators can see
 *                                                               which queue a job is in.
 *   `verified`  COMPLETED                                       Terminal and authorized.
 *   `failed`    FAILED                                          Red. A resting state, not a
 *                                                               tombstone: retry is available.
 *   `cancelled` CANCELLED                                       Grey, terminal, no action needed.
 *
 * No status shares a tone with a status from a different row above, so the
 * palette alone tells an operator whether a job is moving, waiting on them, or
 * finished — which is the point of the mapping.
 */

import type {
  ApprovalDecision,
  EvidenceKind,
  EvidenceStrength,
  JobStatus,
  Provenance,
  RiskLevel,
  Severity,
  StageOutcome,
  VerificationStatus,
} from '../api/types'
import {
  ACTIVE_JOB_STATUSES,
  HUMAN_GATE_JOB_STATUSES,
  TERMINAL_JOB_STATUSES,
} from '../api/types'

/** Semantic tone names. Each maps to a `--tone-*` set in `src/styles.css`. */
export type BadgeTone =
  | 'active'
  | 'review'
  | 'human'
  | 'verified'
  | 'failed'
  | 'cancelled'
  | 'critical'
  | 'high'
  | 'medium'
  | 'low'
  | 'info'
  | 'neutral'
  | 'ai'
  | 'deterministic'
  | 'unsupported'

export interface BadgeMeta {
  label: string
  tone: BadgeTone
  /** Extra guidance rendered as the badge's `title` attribute. */
  description: string
}

const UNKNOWN_DESCRIPTION = 'Value not recognised by this build of the dashboard.'

function unknownMeta(value: string): BadgeMeta {
  return { label: value === '' ? 'UNKNOWN' : value, tone: 'neutral', description: UNKNOWN_DESCRIPTION }
}

/* -------------------------------------------------------------- job status -- */

const JOB_STATUS_TONES: Record<string, BadgeTone> = {
  RECEIVED: 'active',
  QUEUED: 'active',
  PREPARING: 'active',
  ANALYZING: 'active',
  REVIEWING: 'active',
  SECURITY_SCANNING: 'active',
  TESTING: 'active',
  FEATURE_VERIFYING: 'active',
  INVESTIGATING: 'active',
  READY_FOR_REVIEW: 'review',
  WAITING_FOR_HUMAN: 'human',
  COMPLETED: 'verified',
  FAILED: 'failed',
  CANCELLED: 'cancelled',
}

const JOB_STATUS_DESCRIPTIONS: Record<string, string> = {
  RECEIVED: 'Webhook accepted; the job has not been picked up yet.',
  QUEUED: 'Waiting for an orchestrator to claim the job.',
  PREPARING: 'Checking out the pinned commit and building repository context.',
  ANALYZING: 'Repository analyzer building an understanding of the change.',
  REVIEWING: 'Code review agents examining the diff.',
  SECURITY_SCANNING: 'Deterministic security scanners running in the sandbox.',
  TESTING: 'Test suites running in the sandbox.',
  FEATURE_VERIFYING: 'Verifying the stated feature against acceptance criteria.',
  INVESTIGATING: 'Follow-up investigation of open questions.',
  READY_FOR_REVIEW: 'Verification finished; awaiting a human decision.',
  WAITING_FOR_HUMAN: 'Blocked on a human answer to an open question.',
  COMPLETED: 'A human decision has been recorded; the job is finished.',
  FAILED: 'The job failed. It can be re-queued, so this is not necessarily final.',
  CANCELLED: 'The job was cancelled and will not run again.',
}

export function jobStatusMeta(status: JobStatus | string): BadgeMeta {
  const tone = JOB_STATUS_TONES[status]
  const description = JOB_STATUS_DESCRIPTIONS[status]
  if (tone === undefined) return unknownMeta(status)
  return {
    label: humanizeEnum(status),
    tone,
    description: description ?? UNKNOWN_DESCRIPTION,
  }
}

/** True while the orchestrator is expected to be doing work. */
export function isActiveJobStatus(status: JobStatus | string): boolean {
  return (ACTIVE_JOB_STATUSES as readonly string[]).includes(status)
}

/** True when a human decision or answer is what unblocks the job. */
export function isHumanGateStatus(status: JobStatus | string): boolean {
  return (HUMAN_GATE_JOB_STATUSES as readonly string[]).includes(status)
}

/**
 * True once the job will not change again.
 *
 * `FAILED` is intentionally excluded: the re-queue path means a failed job can
 * still move, so the detail page keeps polling it (spec section 51).
 */
export function isTerminalJobStatus(status: JobStatus | string): boolean {
  return (TERMINAL_JOB_STATUSES as readonly string[]).includes(status)
}

/** True when the detail page should keep polling: anything that can still move. */
export function shouldPollJob(status: JobStatus | string | undefined | null): boolean {
  if (status === undefined || status === null) return false
  return !isTerminalJobStatus(status)
}

/* ---------------------------------------------------------------- severity -- */

const SEVERITY_TONES: Record<string, BadgeTone> = {
  CRITICAL: 'critical',
  HIGH: 'high',
  MEDIUM: 'medium',
  LOW: 'low',
  INFO: 'info',
}

const SEVERITY_DESCRIPTIONS: Record<string, string> = {
  CRITICAL: 'Blocks PASS and PASS_WITH_WARNINGS in the verification report.',
  HIGH: 'Blocks PASS in the verification report.',
  MEDIUM: 'Recorded and reported; does not by itself block a recommendation.',
  LOW: 'Minor issue.',
  INFO: 'Informational observation.',
}

export function severityMeta(severity: Severity | string): BadgeMeta {
  const tone = SEVERITY_TONES[severity]
  if (tone === undefined) return unknownMeta(severity)
  return {
    label: severity,
    tone,
    description: SEVERITY_DESCRIPTIONS[severity] ?? UNKNOWN_DESCRIPTION,
  }
}

/* -------------------------------------------------------------------- risk -- */

const RISK_TONES: Record<string, BadgeTone> = {
  CRITICAL: 'critical',
  HIGH: 'high',
  MEDIUM: 'medium',
  LOW: 'low',
}

export function riskMeta(risk: RiskLevel | string | null | undefined): BadgeMeta {
  if (risk === null || risk === undefined || risk === '') {
    return { label: 'UNRATED', tone: 'neutral', description: 'Risk has not been classified yet.' }
  }
  const tone = RISK_TONES[risk]
  if (tone === undefined) return unknownMeta(risk)
  return {
    label: risk,
    tone,
    description: `Risk classification ${risk}: higher risk triggers deeper verification and stricter human gates.`,
  }
}

/* -------------------------------------------------------------- provenance -- */

/**
 * Provenance describes how a finding was reached, and it is the dashboard's
 * primary defence against presenting AI reasoning as a verified fact.
 */
const PROVENANCE_TONES: Record<string, BadgeTone> = {
  DETERMINISTIC: 'deterministic',
  AI_ASSISTED: 'ai',
  AI_ONLY: 'ai',
  UNSUPPORTED: 'unsupported',
}

const PROVENANCE_LABELS: Record<string, string> = {
  DETERMINISTIC: 'DETERMINISTIC',
  AI_ASSISTED: 'AI-ASSISTED',
  AI_ONLY: 'AI-ONLY',
  UNSUPPORTED: 'UNSUPPORTED',
}

const PROVENANCE_DESCRIPTIONS: Record<string, string> = {
  DETERMINISTIC: 'Produced by a deterministic tool. Cannot be argued with.',
  AI_ASSISTED: 'An AI conclusion backed by machine output or direct source inspection.',
  AI_ONLY: 'An AI conclusion resting only on other AI reasoning. Useful, never sufficient alone.',
  UNSUPPORTED: 'No evidence cited. The finding is forced to require human attention.',
}

export function provenanceMeta(provenance: Provenance | string): BadgeMeta {
  const tone = PROVENANCE_TONES[provenance]
  if (tone === undefined) return unknownMeta(provenance)
  return {
    label: PROVENANCE_LABELS[provenance] ?? provenance,
    tone,
    description: PROVENANCE_DESCRIPTIONS[provenance] ?? UNKNOWN_DESCRIPTION,
  }
}

/* --------------------------------------------------------- evidence strength -- */

export const EVIDENCE_STRENGTH_RANK: Record<EvidenceStrength, number> = {
  MACHINE: 3,
  INSPECTION: 2,
  AUTHORITATIVE: 1,
  INFERENCE: 0,
}

const STRENGTH_TONES: Record<string, BadgeTone> = {
  MACHINE: 'deterministic',
  INSPECTION: 'verified',
  AUTHORITATIVE: 'human',
  INFERENCE: 'ai',
}

const STRENGTH_DESCRIPTIONS: Record<string, string> = {
  MACHINE:
    'Deterministic execution: a tool run, test run, git diff, GitHub check, or stored artifact.',
  INSPECTION: 'Direct observation of a file or repository structure, pinned to a path and lines.',
  AUTHORITATIVE: 'A human answer or an approved engineering decision. Outranks anything Scout inferred.',
  INFERENCE: 'An AI claim. Useful, never sufficient on its own.',
}

export function evidenceStrengthMeta(strength: EvidenceStrength | string): BadgeMeta {
  const tone = STRENGTH_TONES[strength]
  if (tone === undefined) return unknownMeta(strength)
  return {
    label: strength,
    tone,
    description: STRENGTH_DESCRIPTIONS[strength] ?? UNKNOWN_DESCRIPTION,
  }
}

export const EVIDENCE_KIND_LABELS: Record<EvidenceKind | string, string> = {
  tool_run: 'Tool run',
  test_run: 'Test run',
  file_range: 'File range',
  git_diff: 'Git diff',
  repository_inspection: 'Repository inspection',
  github_check: 'GitHub check',
  knowledge_document: 'Knowledge document',
  human_answer: 'Human answer',
  agent_run: 'Agent run',
  artifact: 'Artifact',
}

export function evidenceKindLabel(kind: EvidenceKind | string): string {
  return EVIDENCE_KIND_LABELS[kind] ?? kind
}

/* ------------------------------------------------------- stage / verification -- */

const STAGE_OUTCOME_TONES: Record<string, BadgeTone> = {
  PENDING: 'neutral',
  RUNNING: 'active',
  SUCCEEDED: 'verified',
  FAILED: 'failed',
  SKIPPED: 'cancelled',
  CANCELLED: 'cancelled',
}

export function stageOutcomeMeta(outcome: StageOutcome | string): BadgeMeta {
  const tone = STAGE_OUTCOME_TONES[outcome]
  if (tone === undefined) return unknownMeta(outcome)
  return { label: outcome, tone, description: `Stage outcome ${outcome}.` }
}

const VERIFICATION_TONES: Record<string, BadgeTone> = {
  PENDING: 'neutral',
  PASSED: 'verified',
  PASSED_WITH_WARNINGS: 'human',
  FAILED: 'failed',
  INCONCLUSIVE: 'review',
}

export function verificationMeta(status: VerificationStatus | string | null | undefined): BadgeMeta {
  if (status === null || status === undefined || status === '') {
    return { label: 'NOT RUN', tone: 'neutral', description: 'Verification has not produced a status.' }
  }
  const tone = VERIFICATION_TONES[status]
  if (tone === undefined) return unknownMeta(status)
  return { label: status.replace(/_/g, ' '), tone, description: `Verification ${status}.` }
}

const RECOMMENDATION_TONES: Record<string, BadgeTone> = {
  PASS: 'verified',
  PASS_WITH_WARNINGS: 'human',
  REQUIRES_HUMAN_REVIEW: 'review',
  REQUEST_CHANGES: 'high',
  FAIL: 'failed',
}

export function recommendationMeta(recommendation: string | null | undefined): BadgeMeta {
  if (recommendation === null || recommendation === undefined || recommendation === '') {
    return {
      label: 'NO RECOMMENDATION YET',
      tone: 'neutral',
      description: 'Scout has not produced a recommendation for this job yet.',
    }
  }
  const tone = RECOMMENDATION_TONES[recommendation]
  if (tone === undefined) return unknownMeta(recommendation)
  return {
    label: recommendation.replace(/_/g, ' '),
    tone,
    description: 'Scout recommends; a human authorizes. This value is gated by evidence and cannot be widened after the report is generated.',
  }
}

const DECISION_TONES: Record<string, BadgeTone> = {
  APPROVE: 'verified',
  REQUEST_CHANGES: 'high',
  REJECT: 'failed',
}

export function approvalDecisionMeta(decision: ApprovalDecision | string | null | undefined): BadgeMeta {
  if (decision === null || decision === undefined || decision === '') {
    return { label: 'NO DECISION', tone: 'neutral', description: 'No human decision has been recorded.' }
  }
  const tone = DECISION_TONES[decision]
  if (tone === undefined) return unknownMeta(decision)
  return {
    label: decision.replace(/_/g, ' '),
    tone,
    description: `Human authorization decision: ${decision}.`,
  }
}

/* ------------------------------------------------------------------- misc -- */

const ACTOR_TYPE_TONES: Record<string, BadgeTone> = {
  HUMAN: 'human',
  AGENT: 'ai',
  SYSTEM: 'neutral',
  WEBHOOK: 'neutral',
  WORKER: 'active',
}

export function actorTypeMeta(actorType: string): BadgeMeta {
  const tone = ACTOR_TYPE_TONES[actorType]
  if (tone === undefined) return unknownMeta(actorType)
  return {
    label: actorType,
    tone,
    description:
      actorType === 'HUMAN'
        ? 'A human action. Human actions are the only source of authorization.'
        : `Recorded actor type ${actorType}.`,
  }
}

/** Turn `WAITING_FOR_HUMAN` into `Waiting for human` for prose contexts. */
export function humanizeEnum(value: string): string {
  const lower = value.replace(/_/g, ' ').toLowerCase()
  if (lower === '') return value
  return lower.charAt(0).toUpperCase() + lower.slice(1)
}

/** Short SHA for display. Never invents characters that are not there. */
export function shortSha(sha: string | null | undefined, length = 7): string {
  if (sha === null || sha === undefined || sha === '') return '—'
  return sha.slice(0, length)
}

/** Percentage formatting for confidence values in [0, 1]. */
export function formatConfidence(confidence: number): string {
  if (!Number.isFinite(confidence)) return '—'
  const clamped = Math.min(1, Math.max(0, confidence))
  return `${Math.round(clamped * 100)}%`
}

/** Human duration from seconds, e.g. `1m 12s`, `450ms`. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const wholeMinutes = Math.floor(seconds / 60)
  const remainingSeconds = Math.round(seconds % 60)
  if (wholeMinutes < 60) return `${wholeMinutes}m ${remainingSeconds}s`
  const hours = Math.floor(wholeMinutes / 60)
  const minutes = wholeMinutes % 60
  return `${hours}h ${minutes}m`
}

/** Byte size for artifact listings. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(1)} ${units[unitIndex] ?? 'KiB'}`
}

/** Compact integer formatting for token counts. */
export function formatCompactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(
    value,
  )
}

/** USD formatting for LLM cost totals. */
export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (value === 0) return '$0.00'
  if (value < 0.01) return `$${value.toFixed(4)}`
  return `$${value.toFixed(2)}`
}
