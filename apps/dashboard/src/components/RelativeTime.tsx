import { useEffect, useState } from 'react'
import { formatAbsoluteTime, formatRelativeTime } from '../lib/time'

/**
 * A shared clock so every relative timestamp on a page re-renders together
 * instead of each one owning a timer. One interval per interval value.
 */
const subscribers = new Map<number, Set<(now: Date) => void>>()
const timers = new Map<number, ReturnType<typeof setInterval>>()

function subscribe(intervalMs: number, listener: (now: Date) => void): () => void {
  let group = subscribers.get(intervalMs)
  if (!group) {
    group = new Set()
    subscribers.set(intervalMs, group)
  }
  group.add(listener)

  if (!timers.has(intervalMs)) {
    timers.set(
      intervalMs,
      setInterval(() => {
        const now = new Date()
        const current = subscribers.get(intervalMs)
        if (current) for (const callback of current) callback(now)
      }, intervalMs),
    )
  }

  return () => {
    const current = subscribers.get(intervalMs)
    if (!current) return
    current.delete(listener)
    if (current.size === 0) {
      const timer = timers.get(intervalMs)
      if (timer !== undefined) clearInterval(timer)
      timers.delete(intervalMs)
    }
  }
}

export interface RelativeTimeProps {
  /** ISO 8601 timestamp, or null when the event has not happened. */
  value: string | null | undefined
  /** How often to re-render. Defaults to 30s, which is plenty for "3 minutes ago". */
  intervalMs?: number
  /** Rendered when `value` is null/undefined/unparseable. */
  fallback?: string
}

/**
 * Renders an ISO timestamp as "3 minutes ago" with the absolute time in `title`.
 */
export function RelativeTime({
  value,
  intervalMs = 30_000,
  fallback = '—',
}: RelativeTimeProps): JSX.Element {
  const [now, setNow] = useState<Date>(() => new Date())

  useEffect(() => {
    if (value === null || value === undefined) return
    return subscribe(intervalMs, setNow)
  }, [intervalMs, value])

  if (value === null || value === undefined || value === '') {
    return <span className="muted">{fallback}</span>
  }

  return (
    <time dateTime={value} title={formatAbsoluteTime(value)} className="relative-time">
      {formatRelativeTime(value, now)}
    </time>
  )
}
