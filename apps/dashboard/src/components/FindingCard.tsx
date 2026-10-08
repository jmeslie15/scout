import { useId, useState } from 'react'
import type { Finding } from '../api/types'
import { formatConfidence } from '../lib/badges'
import { EvidenceList } from './EvidenceList'
import { ProvenanceBadge } from './ProvenanceBadge'
import { SeverityBadge } from './SeverityBadge'

export interface FindingCardProps {
  finding: Finding
  /** Open the card on first render. Used when a filter already narrowed the list. */
  defaultExpanded?: boolean
  /** Highlighted when deep-linked from a report section. */
  highlighted?: boolean
}

function locationLabel(finding: Finding): string | null {
  if (finding.file === null || finding.file === '') return null
  const { line_start: start, line_end: end } = finding
  if (start === null) return finding.file
  if (end === null || end === start) return `${finding.file}:${start}`
  return `${finding.file}:${start}-${end}`
}

/**
 * One finding, collapsed to its headline and expanded to its full evidence.
 *
 * This is the component that carries the product's central claim, so the
 * distinctions it draws are explicit and textual, not colour-only:
 *
 * - `is_verified_fact: true` renders a "Verified fact" marker; anything else
 *   renders "AI-suggested / not a verified fact".
 * - `provenance` is always shown (DETERMINISTIC / AI-ASSISTED / AI_ONLY /
 *   UNSUPPORTED).
 * - `confidence_capped: true` says so in words and shows the quoted confidence,
 *   because a clamped number is itself evidence of missing support.
 * - `requires_human: true` is called out, since the domain model forces it when
 *   a finding cites no evidence.
 * - The evidence list renders the strength of every reference.
 */
export function FindingCard({ finding, defaultExpanded = false, highlighted = false }: FindingCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const panelId = useId()
  const location = locationLabel(finding)
  const verified = finding.is_verified_fact

  return (
    <article
      className={`finding${highlighted ? ' finding--highlighted' : ''}${
        verified ? ' finding--verified' : ' finding--unverified'
      }`}
      data-verified={verified ? 'true' : 'false'}
      data-provenance={finding.provenance}
      data-severity={finding.severity}
    >
      <div className="finding__head">
        <SeverityBadge severity={finding.severity} />
        <h4 className="finding__title">{finding.title}</h4>
        <span className="finding__category muted">{finding.category}</span>
        <button
          type="button"
          className="button button--ghost button--small finding__toggle"
          aria-expanded={expanded}
          aria-controls={panelId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? 'Hide evidence' : 'Show evidence'}
          <span className="visually-hidden"> for {finding.title}</span>
        </button>
      </div>

      <div className="finding__badges">
        <ProvenanceBadge provenance={finding.provenance} />
        {verified ? (
          <span className="marker marker--verified" title="Provenance is DETERMINISTIC, at least one cited reference is MACHINE strength, and the confidence was not capped.">
            ✓ Verified fact
          </span>
        ) : (
          <span
            className="marker marker--unverified"
            title="Not a verified fact: either the provenance is not DETERMINISTIC, or no MACHINE-strength evidence is cited, or the claimed confidence had to be capped."
          >
            Not a verified fact — AI-suggested or unsupported
          </span>
        )}
        <span className="finding__origin muted">
          {finding.origin === 'TOOL' ? 'Deterministic tool' : 'AI agent'} · source{' '}
          <code>{finding.source}</code>
        </span>
        <span className="finding__confidence">
          confidence {formatConfidence(finding.confidence)}
        </span>
        {finding.confidence_capped ? (
          <span
            className="marker marker--capped"
            title="The claimed confidence exceeded the ceiling allowed by the strongest evidence cited, so Scout clamped it."
          >
            ⚠ Confidence capped by evidence strength
          </span>
        ) : null}
        {finding.requires_human ? (
          <span
            className="marker marker--human"
            title="This finding demands human attention — for example because it cites no evidence at all."
          >
            Requires human review
          </span>
        ) : null}
      </div>

      <div className="finding__summary">
        <p className="finding__description">{finding.description}</p>
        {location !== null ? (
          <p className="finding__location">
            <span className="muted">Location: </span>
            <code>{location}</code>
          </p>
        ) : null}
      </div>

      <div className="finding__panel" id={panelId} hidden={!expanded}>
        <div className="finding__recommendation">
          <h5 className="finding__subhead">Recommendation</h5>
          <p>{finding.recommendation === '' ? 'No recommendation recorded.' : finding.recommendation}</p>
        </div>
        <EvidenceList evidence={finding.evidence} headingLevel={5} />
      </div>

      {!expanded ? (
        <p className="finding__collapsed-hint muted">
          {finding.evidence.length} evidence reference
          {finding.evidence.length === 1 ? '' : 's'} hidden.{' '}
          <button
            type="button"
            className="link-button"
            onClick={() => setExpanded(true)}
            aria-controls={panelId}
            aria-expanded={false}
          >
            Show evidence
          </button>
        </p>
      ) : null}
    </article>
  )
}
