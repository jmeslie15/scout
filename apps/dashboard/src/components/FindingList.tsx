import { useMemo, useState } from 'react'
import type { Finding, Severity } from '../api/types'
import { ALL_SEVERITIES, ALL_PROVENANCE } from '../api/types'
import { severityMeta } from '../lib/badges'
import { EmptyState } from './EmptyState'
import { FindingCard } from './FindingCard'

export interface FindingListProps {
  findings: readonly Finding[]
  /** Shown when nothing is present, to distinguish "none" from "not loaded". */
  emptyTitle?: string
  emptyDescription?: string
  /** Drop the filter bar for embedded lists (for example inside a report section). */
  showFilters?: boolean
}

const SEVERITY_ORDER: Record<Severity, number> = {
  CRITICAL: 0,
  HIGH: 1,
  MEDIUM: 2,
  LOW: 3,
  INFO: 4,
}

type SortKey = 'severity' | 'confidence' | 'created'

/**
 * A list of findings with client-side filtering and sorting.
 *
 * Filtering happens on the client because the API contract exposes findings as
 * part of `JobDetail` with no dedicated findings endpoint and no query
 * parameters — see the API contract gaps section of the README.
 */
export function FindingList({
  findings,
  emptyTitle = 'No findings',
  emptyDescription = 'Scout recorded no findings for this job.',
  showFilters = true,
}: FindingListProps) {
  const [severity, setSeverity] = useState<'ALL' | Severity>('ALL')
  const [provenance, setProvenance] = useState<'ALL' | string>('ALL')
  const [unverifiedOnly, setUnverifiedOnly] = useState(false)
  const [verifiedFactOnly, setVerifiedFactOnly] = useState(false)
  const [requiresHumanOnly, setRequiresHumanOnly] = useState(false)
  const [sortKey, setSortKey] = useState<SortKey>('severity')
  const [query, setQuery] = useState('')

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    const result = findings.filter((finding) => {
      if (severity !== 'ALL' && finding.severity !== severity) return false
      if (provenance !== 'ALL' && finding.provenance !== provenance) return false
      if (verifiedFactOnly && !finding.is_verified_fact) return false
      if (requiresHumanOnly && !finding.requires_human) return false
      if (unverifiedOnly && finding.is_verified_fact) return false
      if (needle !== '') {
        const haystack = `${finding.title} ${finding.description} ${finding.category} ${finding.file ?? ''} ${finding.source}`.toLowerCase()
        if (!haystack.includes(needle)) return false
      }
      return true
    })

    const sorted = [...result]
    sorted.sort((a, b) => {
      if (sortKey === 'severity') {
        const bySeverity = (SEVERITY_ORDER[a.severity] ?? 99) - (SEVERITY_ORDER[b.severity] ?? 99)
        if (bySeverity !== 0) return bySeverity
        return a.title.localeCompare(b.title)
      }
      if (sortKey === 'confidence') return b.confidence - a.confidence
      return (b.created_at ?? '').localeCompare(a.created_at ?? '')
    })
    return sorted
  }, [
    findings,
    severity,
    provenance,
    unverifiedOnly,
    verifiedFactOnly,
    requiresHumanOnly,
    sortKey,
    query,
  ])

  const filtersActive =
    severity !== 'ALL' ||
    provenance !== 'ALL' ||
    unverifiedOnly ||
    verifiedFactOnly ||
    requiresHumanOnly ||
    query.trim() !== ''

  const clearFilters = (): void => {
    setSeverity('ALL')
    setProvenance('ALL')
    setUnverifiedOnly(false)
    setVerifiedFactOnly(false)
    setRequiresHumanOnly(false)
    setQuery('')
  }

  if (findings.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} />
  }

  return (
    <div className="finding-list">
      {showFilters ? (
        <div className="finding-list__filters" role="group" aria-label="Finding filters">
          <label className="field field--inline">
            <span className="field__label">Search</span>
            <input
              type="search"
              className="input"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="title, file, source…"
            />
          </label>
          <label className="field field--inline">
            <span className="field__label">Severity</span>
            <select
              className="select"
              value={severity}
              onChange={(event) => {
                const value = event.target.value
                setSeverity(value === 'ALL' ? 'ALL' : (value as Severity))
              }}
            >
              <option value="ALL">All severities</option>
              {ALL_SEVERITIES.map((value) => (
                <option key={value} value={value}>
                  {severityMeta(value).label}
                </option>
              ))}
            </select>
          </label>
          <label className="field field--inline">
            <span className="field__label">Provenance</span>
            <select
              className="select"
              value={provenance}
              onChange={(event) => setProvenance(event.target.value)}
            >
              <option value="ALL">All provenance</option>
              {ALL_PROVENANCE.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="field field--inline">
            <span className="field__label">Sort</span>
            <select
              className="select"
              value={sortKey}
              onChange={(event) => setSortKey(event.target.value as SortKey)}
            >
              <option value="severity">Severity</option>
              <option value="confidence">Confidence</option>
              <option value="created">Newest</option>
            </select>
          </label>
          <div className="finding-list__toggles">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={verifiedFactOnly}
                onChange={(event) => setVerifiedFactOnly(event.target.checked)}
              />
              <span>Verified facts only</span>
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={unverifiedOnly}
                onChange={(event) => setUnverifiedOnly(event.target.checked)}
              />
              <span>Not verified only</span>
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={requiresHumanOnly}
                onChange={(event) => setRequiresHumanOnly(event.target.checked)}
              />
              <span>Requires human</span>
            </label>
          </div>
          {filtersActive ? (
            <button type="button" className="button button--ghost button--small" onClick={clearFilters}>
              Clear filters
            </button>
          ) : null}
        </div>
      ) : null}

      <p className="finding-list__count muted" aria-live="polite">
        Showing {filtered.length} of {findings.length} finding{findings.length === 1 ? '' : 's'}
        {filtersActive ? ' (filtered)' : ''}.
      </p>

      {filtered.length === 0 ? (
        <EmptyState
          title="No findings match these filters"
          description="Widen the filters to see the other findings on this job."
          action={
            <button type="button" className="button button--small" onClick={clearFilters}>
              Clear filters
            </button>
          }
        />
      ) : (
        <div className="finding-list__items">
          {filtered.map((finding) => (
            <FindingCard key={finding.id} finding={finding} />
          ))}
        </div>
      )}
    </div>
  )
}
