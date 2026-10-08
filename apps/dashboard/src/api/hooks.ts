/**
 * Minimal data-fetching hooks.
 *
 * Scout's engineering policy is "do not introduce infrastructure before it is
 * necessary", so this is deliberately not a query library: one hook that owns
 * loading/error/data, cancellation on unmount, and an optional poll interval.
 * Every page uses it, which is what makes loading, empty, and error handling
 * uniform rather than per-page improvisation.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError } from './client'

export type AsyncStatus = 'loading' | 'success' | 'error'

export interface AsyncState<T> {
  data: T | null
  error: ApiError | null
  status: AsyncStatus
  /** True while a refetch is in flight and previous data is still displayed. */
  refreshing: boolean
  /** Re-run the fetcher now. Safe to call from an event handler. */
  refetch: () => void
  /**
   * Replace the current data without a round trip.
   *
   * Used after a write: POST /approve returns the updated job, and putting that
   * response straight into the cache avoids a second GET and the flicker that
   * comes with it.
   */
  setData: (value: T | null) => void
}

export interface UseAsyncOptions {
  /** Poll interval in milliseconds. 0 or undefined disables polling. */
  pollMs?: number
  /** Set false to skip the request entirely (for dependent/conditional fetches). */
  enabled?: boolean
}

/**
 * Run `fetcher` whenever `deps` change and expose the result with explicit
 * loading/error state. Errors are normalised to `ApiError`; anything else
 * thrown by `fetcher` is wrapped so the UI never has to guess.
 */
export function useAsync<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  options: UseAsyncOptions = {},
): AsyncState<T> {
  const { pollMs = 0, enabled = true } = options

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [status, setStatus] = useState<AsyncStatus>(enabled ? 'loading' : 'success')
  const [refreshing, setRefreshing] = useState(false)
  const [nonce, setNonce] = useState(0)

  // Keep the latest fetcher without making it a dependency of the effect: an
  // inline arrow function passed by a caller would otherwise refetch forever.
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const hasLoadedRef = useRef(false)

  const refetch = useCallback(() => {
    setNonce((value) => value + 1)
  }, [])

  useEffect(() => {
    if (!enabled) {
      setStatus('success')
      setData(null)
      setError(null)
      return
    }

    const controller = new AbortController()
    let cancelled = false

    const run = async (): Promise<void> => {
      if (hasLoadedRef.current) setRefreshing(true)
      else setStatus('loading')
      try {
        const result = await fetcherRef.current(controller.signal)
        if (cancelled) return
        setData(result)
        setError(null)
        setStatus('success')
      } catch (caught) {
        if (cancelled) return
        const apiError =
          caught instanceof ApiError
            ? caught
            : new ApiError({
                message: caught instanceof Error ? caught.message : String(caught),
                status: 0,
                kind: 'network',
                body: null,
                path: 'unknown',
              })
        setError(apiError)
        setStatus('error')
      } finally {
        if (!cancelled) {
          hasLoadedRef.current = true
          setRefreshing(false)
        }
      }
    }

    void run()

    let timer: ReturnType<typeof setInterval> | undefined
    if (pollMs > 0) {
      timer = setInterval(() => {
        void run()
      }, pollMs)
    }

    return () => {
      cancelled = true
      controller.abort()
      if (timer !== undefined) clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, enabled, pollMs])

  return { data, error, status, refreshing, refetch, setData }
}
