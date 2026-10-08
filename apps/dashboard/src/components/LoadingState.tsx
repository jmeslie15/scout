export interface LoadingStateProps {
  /** What is being loaded, e.g. "jobs" — shown to the operator, not just a spinner. */
  label?: string
  /** `inline` is a short row; `block` fills a panel. */
  variant?: 'inline' | 'block'
}

/**
 * Loading placeholder. Always names what is loading so a stuck request is
 * diagnosable from the screen rather than from devtools.
 */
export function LoadingState({ label = 'Loading', variant = 'block' }: LoadingStateProps) {
  return (
    <div
      className={variant === 'inline' ? 'loading loading--inline' : 'loading loading--block'}
      role="status"
      aria-live="polite"
    >
      <span className="spinner" aria-hidden="true" />
      <span className="loading__label">{label}…</span>
    </div>
  )
}
