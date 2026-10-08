import type { JobStatus } from '../api/types'
import { isActiveJobStatus, jobStatusMeta } from '../lib/badges'
import { Badge } from './Badge'

export interface StatusBadgeProps {
  status: JobStatus | string
}

/**
 * Job status badge.
 *
 * The colour mapping (documented in full in `src/lib/badges.ts`) separates
 * in-flight states from READY_FOR_REVIEW, WAITING_FOR_HUMAN, FAILED, CANCELLED,
 * and COMPLETED. In-flight states get a pulsing dot because they are the only
 * statuses that will change without operator action.
 */
export function StatusBadge({ status }: StatusBadgeProps) {
  const meta = jobStatusMeta(status)
  return (
    <Badge tone={meta.tone} description={meta.description} pulse={isActiveJobStatus(status)}>
      {meta.label}
    </Badge>
  )
}
