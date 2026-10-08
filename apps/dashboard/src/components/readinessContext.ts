import { createContext, useContext } from 'react'
import type { ApiError } from '../api/client'
import type { ReadinessResponse } from '../api/types'

/**
 * Readiness is fetched once by `AppShell` and shared with pages through context
 * so the dashboard's degradation banner and the shell's header indicator can
 * never disagree. The context value is `null` by default (rather than throwing),
 * which lets a test or a page render without a shell.
 */
export interface ReadinessContextValue {
  readiness: ReadinessResponse | null
  error: ApiError | null
  loading: boolean
  refetch: () => void
}

export const ReadinessContext = createContext<ReadinessContextValue | null>(null)

export function useReadiness(): ReadinessContextValue | null {
  return useContext(ReadinessContext)
}
