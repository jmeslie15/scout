import { describe, expect, it } from 'vitest'
import {
  approvalDecisionMeta,
  evidenceStrengthMeta,
  formatBytes,
  formatConfidence,
  formatDuration,
  isActiveJobStatus,
  isHumanGateStatus,
  isTerminalJobStatus,
  jobStatusMeta,
  provenanceMeta,
  recommendationMeta,
  riskMeta,
  severityMeta,
  shortSha,
  shouldPollJob,
} from './badges'
import { ALL_JOB_STATUSES, ALL_SEVERITIES, ALL_RISK_LEVELS, ALL_PROVENANCE } from '../api/types'

/**
 * The badge mappings are tested because they encode the product's most important
 * presentation decision: an operator must be able to tell an in-flight job from
 * one waiting on them, and an AI-only finding from a verified fact, without
 * relying on colour alone or on hover text.
 */

describe('jobStatusMeta', () => {
  it('gives every job status a tone, so nothing renders blank', () => {
    for (const status of ALL_JOB_STATUSES) {
      const meta = jobStatusMeta(status)
      expect(meta.label).not.toBe('')
      expect(meta.tone).not.toBe('neutral')
      expect(meta.description).not.toBe('')
    }
  })

  it('distinguishes active, review, human-gate, verified, failed, and cancelled states', () => {
    expect(jobStatusMeta('TESTING').tone).toBe('active')
    expect(jobStatusMeta('READY_FOR_REVIEW').tone).toBe('review')
    expect(jobStatusMeta('WAITING_FOR_HUMAN').tone).toBe('human')
    expect(jobStatusMeta('COMPLETED').tone).toBe('verified')
    expect(jobStatusMeta('FAILED').tone).toBe('failed')
    expect(jobStatusMeta('CANCELLED').tone).toBe('cancelled')

    // No two of those six situations may share a tone.
    const tones = [
      'TESTING',
      'READY_FOR_REVIEW',
      'WAITING_FOR_HUMAN',
      'COMPLETED',
      'FAILED',
      'CANCELLED',
    ].map((status) => jobStatusMeta(status).tone)
    expect(new Set(tones).size).toBe(tones.length)
  })

  it('falls back safely for a status this build does not know', () => {
    const meta = jobStatusMeta('SOMETHING_NEW')
    expect(meta.label).toBe('SOMETHING_NEW')
    expect(meta.tone).toBe('neutral')
  })
})

describe('status predicates', () => {
  it('classifies active, human-gated, and terminal statuses', () => {
    expect(isActiveJobStatus('QUEUED')).toBe(true)
    expect(isActiveJobStatus('READY_FOR_REVIEW')).toBe(false)
    expect(isHumanGateStatus('READY_FOR_REVIEW')).toBe(true)
    expect(isHumanGateStatus('WAITING_FOR_HUMAN')).toBe(true)
    expect(isHumanGateStatus('TESTING')).toBe(false)
    expect(isTerminalJobStatus('COMPLETED')).toBe(true)
    expect(isTerminalJobStatus('CANCELLED')).toBe(true)
    // FAILED is a resting state, not a tombstone: retry can still move it.
    expect(isTerminalJobStatus('FAILED')).toBe(false)
  })

  it('polls everything that can still move and stops at a tombstone', () => {
    expect(shouldPollJob('TESTING')).toBe(true)
    expect(shouldPollJob('WAITING_FOR_HUMAN')).toBe(true)
    expect(shouldPollJob('FAILED')).toBe(true)
    expect(shouldPollJob('COMPLETED')).toBe(false)
    expect(shouldPollJob('CANCELLED')).toBe(false)
    expect(shouldPollJob(null)).toBe(false)
    expect(shouldPollJob(undefined)).toBe(false)
  })
})

describe('severityMeta', () => {
  it('maps every severity to a distinct tone', () => {
    const tones = ALL_SEVERITIES.map((severity) => severityMeta(severity).tone)
    expect(new Set(tones).size).toBe(ALL_SEVERITIES.length)
    expect(severityMeta('CRITICAL').tone).toBe('critical')
    expect(severityMeta('INFO').tone).toBe('info')
  })
})

describe('riskMeta', () => {
  it('maps every risk level and gives an unrated job its own label', () => {
    for (const risk of ALL_RISK_LEVELS) {
      expect(riskMeta(risk).tone).not.toBe('neutral')
    }
    expect(riskMeta(null).label).toBe('UNRATED')
    expect(riskMeta(undefined).tone).toBe('neutral')
  })
})

describe('provenanceMeta', () => {
  it('maps every provenance value and keeps AI-only distinct from deterministic', () => {
    for (const provenance of ALL_PROVENANCE) {
      expect(provenanceMeta(provenance).tone).not.toBe('neutral')
    }
    expect(provenanceMeta('DETERMINISTIC').tone).toBe('deterministic')
    expect(provenanceMeta('AI_ONLY').tone).toBe('ai')
    expect(provenanceMeta('UNSUPPORTED').tone).toBe('unsupported')
    expect(provenanceMeta('AI_ONLY').label).toBe('AI-ONLY')
  })
})

describe('evidenceStrengthMeta', () => {
  it('keeps the four strengths visibly distinct', () => {
    const strengths = ['MACHINE', 'INSPECTION', 'AUTHORITATIVE', 'INFERENCE'] as const
    const tones = strengths.map((strength) => evidenceStrengthMeta(strength).tone)
    expect(new Set(tones).size).toBe(strengths.length)
    expect(evidenceStrengthMeta('MACHINE').tone).toBe('deterministic')
    expect(evidenceStrengthMeta('INFERENCE').tone).toBe('ai')
  })
})

describe('recommendationMeta and approvalDecisionMeta', () => {
  it('distinguishes a pass from a pass with warnings from a request for changes', () => {
    expect(recommendationMeta('PASS').tone).toBe('verified')
    expect(recommendationMeta('PASS_WITH_WARNINGS').tone).toBe('human')
    expect(recommendationMeta('REQUEST_CHANGES').tone).toBe('high')
    expect(recommendationMeta('FAIL').tone).toBe('failed')
    expect(recommendationMeta(null).label).toBe('NO RECOMMENDATION YET')
  })

  it('maps approval decisions', () => {
    expect(approvalDecisionMeta('APPROVE').tone).toBe('verified')
    expect(approvalDecisionMeta('REJECT').tone).toBe('failed')
    expect(approvalDecisionMeta(null).label).toBe('NO DECISION')
  })
})

describe('formatting helpers', () => {
  it('shortens a SHA without inventing characters', () => {
    expect(shortSha('abcdef1234567890')).toBe('abcdef1')
    expect(shortSha('abc')).toBe('abc')
    expect(shortSha(null)).toBe('—')
    expect(shortSha('')).toBe('—')
  })

  it('formats confidence as a clamped percentage', () => {
    expect(formatConfidence(1)).toBe('100%')
    expect(formatConfidence(0.65)).toBe('65%')
    expect(formatConfidence(-1)).toBe('0%')
    expect(formatConfidence(9)).toBe('100%')
    expect(formatConfidence(Number.NaN)).toBe('—')
  })

  it('formats durations across magnitudes', () => {
    expect(formatDuration(0.45)).toBe('450ms')
    expect(formatDuration(12.34)).toBe('12.3s')
    expect(formatDuration(72)).toBe('1m 12s')
    expect(formatDuration(3720)).toBe('1h 2m')
    expect(formatDuration(null)).toBe('—')
  })

  it('formats bytes across magnitudes', () => {
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(2048)).toBe('2.0 KiB')
    expect(formatBytes(null)).toBe('—')
  })
})
