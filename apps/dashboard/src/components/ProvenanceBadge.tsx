import type { Provenance } from '../api/types'
import { provenanceMeta } from '../lib/badges'
import { Badge } from './Badge'

export interface ProvenanceBadgeProps {
  provenance: Provenance | string
}

/**
 * How a finding was reached.
 *
 * This badge is load-bearing, not decorative: `AI_ONLY` and `UNSUPPORTED`
 * findings must never look like `DETERMINISTIC` ones, so the tone for each is
 * distinct and the label is never abbreviated.
 */
export function ProvenanceBadge({ provenance }: ProvenanceBadgeProps) {
  const meta = provenanceMeta(provenance)
  return (
    <Badge tone={meta.tone} description={meta.description}>
      {meta.label}
    </Badge>
  )
}
