import type { RiskLevel } from '../api/types'
import { riskMeta } from '../lib/badges'
import { Badge } from './Badge'

export interface RiskBadgeProps {
  risk: RiskLevel | string | null | undefined
}

/**
 * Risk classification. A job that has not been classified yet renders as
 * `UNRATED` rather than an empty cell, because an absent risk level is itself
 * information an operator should see.
 */
export function RiskBadge({ risk }: RiskBadgeProps) {
  const meta = riskMeta(risk)
  return (
    <Badge tone={meta.tone} description={meta.description}>
      {meta.label}
    </Badge>
  )
}
