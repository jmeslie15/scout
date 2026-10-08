import type { FindingCounts, Severity } from '../api/types'
import { ALL_SEVERITIES } from '../api/types'
import { severityMeta } from '../lib/badges'

export interface SeverityCountsProps {
  counts: FindingCounts | Record<string, number> | null | undefined
  /** Hide severities whose count is zero. Default true. */
  hideZeros?: boolean
}

/**
 * Compact severity tally, used in the jobs table and the dashboard breakdown.
 *
 * Every count carries its severity name, not just a colour, so the tally is
 * readable without relying on colour perception.
 */
export function SeverityCounts({ counts, hideZeros = true }: SeverityCountsProps) {
  if (counts === null || counts === undefined) {
    return <span className="muted">—</span>
  }

  const entries = ALL_SEVERITIES.map((severity) => ({
    severity,
    count: counts[severity] ?? 0,
  })).filter((entry) => !hideZeros || entry.count > 0)

  if (entries.length === 0) {
    return (
      <span className="severity-counts severity-counts--zero" title="No findings recorded">
        0
      </span>
    )
  }

  return (
    <span className="severity-counts">
      {entries.map((entry) => {
        const meta = severityMeta(entry.severity as Severity)
        return (
          <span
            key={entry.severity}
            className={`severity-count severity-count--${meta.tone}`}
            title={`${entry.count} ${entry.severity} finding${entry.count === 1 ? '' : 's'}`}
          >
            <span className="severity-count__label">{entry.severity[0]}</span>
            <span className="severity-count__value">{entry.count}</span>
            <span className="visually-hidden">{entry.severity}</span>
          </span>
        )
      })}
    </span>
  )
}
