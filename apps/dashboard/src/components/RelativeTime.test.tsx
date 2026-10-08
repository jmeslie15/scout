import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RelativeTime } from './RelativeTime'
import { formatAbsoluteTime } from '../lib/time'

describe('RelativeTime', () => {
  it('renders a human relative string and puts the absolute time in the title', () => {
    const iso = new Date(Date.now() - 3 * 60_000).toISOString()
    render(<RelativeTime value={iso} />)

    const element = screen.getByText(/minutes? ago/)
    expect(element.tagName).toBe('TIME')
    expect(element).toHaveAttribute('datetime', iso)
    expect(element).toHaveAttribute('title', formatAbsoluteTime(iso))
  })

  it('renders the fallback when the value is missing', () => {
    render(<RelativeTime value={null} fallback="never" />)
    expect(screen.getByText('never')).toBeInTheDocument()
  })
})
