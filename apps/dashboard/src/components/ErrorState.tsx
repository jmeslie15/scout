import { ApiError, apiBaseUrl } from '../api/client'

export interface ErrorStateProps {
  /** The failure. Anything non-`ApiError` is rendered as a generic error. */
  error: ApiError | Error | null
  /** Retry action. When omitted the button is not rendered. */
  onRetry?: () => void
  /** Short description of what failed, e.g. "the job list". */
  context?: string
}

/**
 * Failure panel.
 *
 * Two things are non-negotiable here: the HTTP status is always shown (so a 404
 * reads as "not found" rather than "something went wrong"), and a Retry button
 * is always offered when the caller can retry. A spinner that never resolves on
 * a 500 is a bug, so this component is what replaces the spinner.
 */
export function ErrorState({ error, onRetry, context }: ErrorStateProps) {
  const isApiError = error instanceof ApiError
  const status = isApiError ? error.status : null
  const title =
    status === 404
      ? 'Not found'
      : isApiError && error.isUnreachable
        ? 'Cannot reach the Scout API'
        : 'Something went wrong'

  const detail = isApiError
    ? error.displayMessage
    : error !== null
      ? error.message
      : 'Unknown error.'

  return (
    <div className="error-state" role="alert">
      <div className="error-state__head">
        <h3 className="error-state__title">{title}</h3>
        {status !== null && status > 0 ? (
          <span className="error-state__status" title="HTTP status returned by the API">
            HTTP {status}
          </span>
        ) : null}
      </div>
      {context !== undefined ? <p className="error-state__context">Failed to load {context}.</p> : null}
      <p className="error-state__detail">{detail}</p>
      {isApiError && error.path !== '' && error.path !== 'unknown' ? (
        <p className="error-state__path">
          <span className="muted">Request: </span>
          <code>
            {apiBaseUrl()}
            {error.path}
          </code>
        </p>
      ) : null}
      {onRetry !== undefined ? (
        <button type="button" className="button" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  )
}
