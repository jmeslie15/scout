import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'

export type SearchParamsUpdate = Record<string, string | number | null | undefined>

/**
 * Read and update query-string state.
 *
 * Filters live in the URL rather than in component state because operators
 * paste links to each other ("here are the failing jobs in that repo"), and a
 * `useState` filter would not survive that. Updating replaces defaults with
 * absent parameters so a filtered URL stays readable, and it uses `replace` to
 * keep the back button useful instead of walking through every keystroke.
 */
export function useQueryState(): {
  get: (key: string, fallback?: string) => string | null
  getNumber: (key: string, fallback: number) => number
  update: (changes: SearchParamsUpdate) => void
  clear: (keys: readonly string[]) => void
  params: URLSearchParams
} {
  const [params, setParams] = useSearchParams()

  const get = useCallback(
    (key: string, fallback?: string): string | null => {
      const value = params.get(key)
      if (value === null || value === '') return fallback ?? null
      return value
    },
    [params],
  )

  const getNumber = useCallback(
    (key: string, fallback: number): number => {
      const raw = params.get(key)
      if (raw === null || raw === '') return fallback
      const parsed = Number(raw)
      return Number.isFinite(parsed) ? parsed : fallback
    },
    [params],
  )

  const update = useCallback(
    (changes: SearchParamsUpdate): void => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current)
          for (const [key, value] of Object.entries(changes)) {
            if (value === null || value === undefined || value === '') next.delete(key)
            else next.set(key, String(value))
          }
          return next
        },
        { replace: true },
      )
    },
    [setParams],
  )

  const clear = useCallback(
    (keys: readonly string[]): void => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current)
          for (const key of keys) next.delete(key)
          return next
        },
        { replace: true },
      )
    },
    [setParams],
  )

  return { get, getNumber, update, clear, params }
}
