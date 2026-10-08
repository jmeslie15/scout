import type { ReactNode } from 'react'

export interface EmptyStateProps {
  title: string
  /** Explain what an empty result means; for filters, say how to widen it. */
  description?: ReactNode
  /** Optional action, e.g. "Clear filters". */
  action?: ReactNode
}

/**
 * Empty result placeholder.
 *
 * An empty list is a legitimate answer, so it gets an explanation instead of an
 * empty table body. When the emptiness is caused by a filter, the caller passes
 * a "clear filters" action.
 */
export function EmptyState({ title, description, action }: EmptyStateProps) {
  return (
    <div className="empty-state">
      <p className="empty-state__title">{title}</p>
      {description !== undefined ? (
        <p className="empty-state__description">{description}</p>
      ) : null}
      {action !== undefined ? <div className="empty-state__action">{action}</div> : null}
    </div>
  )
}
