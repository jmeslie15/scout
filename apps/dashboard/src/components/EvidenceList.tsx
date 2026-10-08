import type { EvidenceReference } from '../api/types'
import { evidenceKindLabel, evidenceStrengthMeta } from '../lib/badges'
import { Badge } from './Badge'

export interface EvidenceListProps {
  evidence: readonly EvidenceReference[]
  /** Shown when there is no evidence at all. */
  emptyMessage?: string
  /** Heading level for the section title, so the list nests correctly. */
  headingLevel?: 3 | 4 | 5
}

function rangeLabel(entry: EvidenceReference): string | null {
  if (entry.file_path === null || entry.file_path === '') return null
  const { line_start: start, line_end: end } = entry
  if (start === null) return entry.file_path
  if (end === null || end === start) return `${entry.file_path}:${start}`
  return `${entry.file_path}:${start}-${end}`
}

/**
 * Render a finding's (or question's) evidence references.
 *
 * Each entry states its **strength** — MACHINE, INSPECTION, AUTHORITATIVE, or
 * INFERENCE — because strength, not the presence of a citation, is what decides
 * whether a claim can be treated as verified. Every field the contract supplies
 * is rendered: kind, reference id, summary, file/line range, excerpt, artifact
 * id, and url.
 */
export function EvidenceList({
  evidence,
  emptyMessage = 'No evidence cited. A claim without evidence cannot be treated as verified.',
  headingLevel = 4,
}: EvidenceListProps) {
  const Heading = `h${headingLevel}` as const

  if (evidence.length === 0) {
    return (
      <div className="evidence evidence--empty">
        <Heading className="evidence__title">Evidence</Heading>
        <p className="evidence__none">{emptyMessage}</p>
      </div>
    )
  }

  return (
    <div className="evidence">
      <Heading className="evidence__title">
        Evidence <span className="muted">({evidence.length})</span>
      </Heading>
      <ul className="evidence__list">
        {evidence.map((entry, index) => {
          const strength = evidenceStrengthMeta(entry.strength)
          const location = rangeLabel(entry)
          return (
            <li key={`${entry.kind}:${entry.reference_id}:${index}`} className="evidence__item">
              <div className="evidence__head">
                <Badge tone={strength.tone} description={strength.description}>
                  {strength.label}
                </Badge>
                <span className="evidence__kind">{evidenceKindLabel(entry.kind)}</span>
                <code className="evidence__ref" title="Evidence reference id">
                  {entry.reference_id}
                </code>
              </div>
              <p className="evidence__summary">{entry.summary}</p>
              {location !== null ? (
                <p className="evidence__location">
                  <span className="muted">Location: </span>
                  <code>{location}</code>
                </p>
              ) : null}
              {entry.excerpt !== null && entry.excerpt !== '' ? (
                <pre className="evidence__excerpt">{entry.excerpt}</pre>
              ) : null}
              <div className="evidence__links">
                {entry.artifact_id !== null && entry.artifact_id !== '' ? (
                  <span className="evidence__meta">
                    <span className="muted">Artifact: </span>
                    <code>{entry.artifact_id}</code>
                  </span>
                ) : null}
                {entry.url !== null && entry.url !== '' ? (
                  <a href={entry.url} target="_blank" rel="noreferrer noopener">
                    Open source
                  </a>
                ) : null}
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
