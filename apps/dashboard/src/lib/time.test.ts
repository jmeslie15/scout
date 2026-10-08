import { describe, expect, it } from 'vitest'
import { formatAbsoluteTime, formatRelativeTime, parseIsoTimestamp } from './time'

const NOW = new Date('2024-05-01T12:00:00.000Z')

describe('formatRelativeTime', () => {
  it('renders recent timestamps as "just now"', () => {
    expect(formatRelativeTime('2024-05-01T11:59:55.000Z', NOW)).toBe('just now')
  })

  it('renders minutes, hours, and days with correct pluralisation', () => {
    expect(formatRelativeTime('2024-05-01T11:57:00.000Z', NOW)).toBe('3 minutes ago')
    expect(formatRelativeTime('2024-05-01T11:59:00.000Z', NOW)).toBe('1 minute ago')
    expect(formatRelativeTime('2024-05-01T09:00:00.000Z', NOW)).toBe('3 hours ago')
    expect(formatRelativeTime('2024-04-29T12:00:00.000Z', NOW)).toBe('2 days ago')
    expect(formatRelativeTime('2024-04-01T12:00:00.000Z', NOW)).toBe('1 month ago')
  })

  it('renders a future timestamp as "in …" rather than a negative "ago"', () => {
    expect(formatRelativeTime('2024-05-01T12:03:00.000Z', NOW)).toBe('in 3 minutes')
  })

  it('returns an em dash for missing or unparseable values', () => {
    expect(formatRelativeTime(null, NOW)).toBe('—')
    expect(formatRelativeTime(undefined, NOW)).toBe('—')
    expect(formatRelativeTime('', NOW)).toBe('—')
    expect(formatRelativeTime('not a date', NOW)).toBe('—')
  })
})

describe('parseIsoTimestamp', () => {
  it('parses a valid timestamp and rejects garbage', () => {
    expect(parseIsoTimestamp('2024-05-01T12:00:00Z')?.toISOString()).toBe('2024-05-01T12:00:00.000Z')
    expect(parseIsoTimestamp('nonsense')).toBeNull()
    expect(parseIsoTimestamp(null)).toBeNull()
  })
})

describe('formatAbsoluteTime', () => {
  it('produces a non-empty absolute string and a fallback for bad input', () => {
    expect(formatAbsoluteTime('2024-05-01T12:00:00Z').length).toBeGreaterThan(0)
    expect(formatAbsoluteTime(null)).toBe('unknown time')
  })
})
