/**
 * Relative and absolute time formatting for ISO 8601 timestamps.
 *
 * `formatRelativeTime` is pure and takes an explicit `now` so it is testable
 * without faking the clock. `RelativeTime` renders the relative string with the
 * absolute timestamp in the element's `title`, which is what makes a table cell
 * like "3 minutes ago" auditable rather than merely friendly.
 */

/** Parse an ISO timestamp. Returns null for anything unparseable. */
export function parseIsoTimestamp(value: string | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR
const WEEK = 7 * DAY
const MONTH = 30 * DAY
const YEAR = 365 * DAY

function plural(count: number, unit: string): string {
  return `${count} ${unit}${count === 1 ? '' : 's'} ago`
}

/**
 * Render a timestamp relative to `now`.
 *
 * Future timestamps (clock skew between the API host and the browser) read as
 * `in 3 minutes` rather than a negative "ago", which would look like a bug.
 */
export function formatRelativeTime(
  value: string | null | undefined,
  now: Date = new Date(),
): string {
  const date = parseIsoTimestamp(value)
  if (date === null) return '—'

  const deltaMs = now.getTime() - date.getTime()
  const future = deltaMs < 0
  const magnitude = Math.abs(deltaMs)

  const render = (text: string): string => {
    if (!future) return text
    return text.replace(/ ago$/, '').replace(/^/, 'in ')
  }

  if (magnitude < 10_000) return render('just now')
  if (magnitude < MINUTE) return render(plural(Math.floor(magnitude / 1000), 'second'))
  if (magnitude < HOUR) return render(plural(Math.floor(magnitude / MINUTE), 'minute'))
  if (magnitude < DAY) return render(plural(Math.floor(magnitude / HOUR), 'hour'))
  if (magnitude < WEEK) return render(plural(Math.floor(magnitude / DAY), 'day'))
  if (magnitude < MONTH) return render(plural(Math.floor(magnitude / WEEK), 'week'))
  if (magnitude < YEAR) return render(plural(Math.floor(magnitude / MONTH), 'month'))
  return render(plural(Math.floor(magnitude / YEAR), 'year'))
}

/** Absolute, unambiguous local rendering used for `title` attributes. */
export function formatAbsoluteTime(value: string | null | undefined): string {
  const date = parseIsoTimestamp(value)
  if (date === null) return 'unknown time'
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZoneName: 'short',
  })
}
