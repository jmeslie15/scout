import type { ReactNode } from 'react'
import { ApiError } from '../api/client'
import { ErrorState } from './ErrorState'
import { LoadingState } from './LoadingState'

export interface AsyncBoundaryProps {
  state: {
    data: unknown
    error: ApiError | null
    status: 'loading' | 'success' | 'error'
    refetch: () => void
  }
  /** What is being loaded, used in both the loading and error copy. */
  context: string
  /** Also show the error panel when data is present but stale. Default false. */
  errorOverridesData?: boolean
  children: ReactNode
}

/**
 * Renders the three fetch states consistently.
 *
 * Centralising this is what makes it structurally hard to ship a spinner that
 * never resolves: there is exactly one place that decides what a failed fetch
 * looks like, and it always offers a retry.
 */
export function AsyncBoundary({
  state,
  context,
  errorOverridesData = false,
  children,
}: AsyncBoundaryProps) {
  if (state.status === 'loading' && state.data === null) {
    return <LoadingState label={`Loading ${context}`} />
  }
  if (state.error !== null && (state.data === null || errorOverridesData)) {
    return <ErrorState error={state.error} context={context} onRetry={state.refetch} />
  }
  return <>{children}</>
}
