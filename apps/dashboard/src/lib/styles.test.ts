import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import {
  actorTypeMeta,
  approvalDecisionMeta,
  evidenceStrengthMeta,
  jobStatusMeta,
  provenanceMeta,
  recommendationMeta,
  riskMeta,
  severityMeta,
  stageOutcomeMeta,
  verificationMeta,
} from './badges'
import {
  ALL_JOB_STATUSES,
  ALL_PROVENANCE,
  ALL_RISK_LEVELS,
  ALL_SEVERITIES,
} from '../api/types'

/**
 * Colour is applied by class, and class names are built from tone strings. A
 * typo or a missing rule would silently render an unstyled badge — white text on
 * white, or a badge with no background at all — which is exactly the kind of
 * failure that survives a type check and a unit test of the mapping function.
 *
 * This suite reads `src/styles.css` and asserts that every tone the code can
 * produce has a matching `.badge--<tone>` rule.
 */

// Vitest's root is the dashboard package, so this resolves to the single
// stylesheet that ships with the app.
const css = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')

function collectTones(): Set<string> {
  const tones = new Set<string>()
  for (const status of ALL_JOB_STATUSES) tones.add(jobStatusMeta(status).tone)
  for (const severity of ALL_SEVERITIES) tones.add(severityMeta(severity).tone)
  for (const risk of ALL_RISK_LEVELS) tones.add(riskMeta(risk).tone)
  for (const provenance of ALL_PROVENANCE) tones.add(provenanceMeta(provenance).tone)
  for (const strength of ['MACHINE', 'INSPECTION', 'AUTHORITATIVE', 'INFERENCE'] as const) {
    tones.add(evidenceStrengthMeta(strength).tone)
  }
  tones.add(riskMeta(null).tone)
  tones.add(recommendationMeta(null).tone)
  for (const recommendation of [
    'PASS',
    'PASS_WITH_WARNINGS',
    'REQUIRES_HUMAN_REVIEW',
    'REQUEST_CHANGES',
    'FAIL',
  ]) {
    tones.add(recommendationMeta(recommendation).tone)
  }
  for (const decision of ['APPROVE', 'REQUEST_CHANGES', 'REJECT']) {
    tones.add(approvalDecisionMeta(decision).tone)
  }
  for (const outcome of ['PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED']) {
    tones.add(stageOutcomeMeta(outcome).tone)
  }
  for (const status of ['PENDING', 'PASSED', 'PASSED_WITH_WARNINGS', 'FAILED', 'INCONCLUSIVE']) {
    tones.add(verificationMeta(status).tone)
  }
  for (const actor of ['SYSTEM', 'AGENT', 'HUMAN', 'WEBHOOK', 'WORKER']) {
    tones.add(actorTypeMeta(actor).tone)
  }
  return tones
}

describe('CSS tone coverage', () => {
  const tones = [...collectTones()]

  it('produces a non-trivial set of tones', () => {
    expect(tones.length).toBeGreaterThanOrEqual(10)
  })

  for (const tone of tones) {
    it(`defines .badge--${tone}`, () => {
      expect(css).toContain(`.badge--${tone}`)
    })
  }

  it('defines a background tint for every .badge-- rule it declares', () => {
    // Guards against a rule that exists but forgot its `background`, which would
    // leave the badge label floating on the panel background. Selectors may be
    // grouped (`.badge--info, .badge--neutral { … }`), so each name in the group
    // is checked against the shared declaration block.
    const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    let checked = 0
    for (const rule of rules) {
      const [, selectors, body] = rule
      if (selectors === undefined || body === undefined) continue
      const names = [...selectors.matchAll(/\.badge--([a-z]+)/g)].map((match) => match[1])
      if (names.length === 0) continue
      expect(body, `.badge--${names.join(', .badge--')} must set a background`).toContain(
        'background:',
      )
      expect(body, `.badge--${names.join(', .badge--')} must set a color`).toContain('color:')
      checked += names.length
    }
    expect(checked).toBeGreaterThanOrEqual(tones.length)
  })

  it('defines dark-mode overrides for the tone custom properties', () => {
    expect(css).toContain('@media (prefers-color-scheme: dark)')
    expect(css).toContain('--tone-active-accent')
    expect(css).toContain('--tone-human-ink')
    expect(css).toContain('--tone-deterministic-ink')
  })
})
