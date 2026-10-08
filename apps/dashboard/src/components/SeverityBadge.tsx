import type { Severity } from '../api/types'
import { severityMeta } from '../lib/badges'
import { Badge } from './Badge'

export interface SeverityBadgeProps {
  severity: Severity | string
}

/** Finding/question severity. CRITICAL and HIGH are the ones that gate reports. */
export function SeverityBadge({ severity }: SeverityBadgeProps) {
  const meta = severityMeta(severity)
  return (
    <Badge tone={meta.tone} description={meta.description}>
      {meta.label}
    </Badge>
  )
}
