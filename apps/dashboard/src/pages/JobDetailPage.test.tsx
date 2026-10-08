import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { JobDetailPage } from './JobDetailPage'
import { artifact, jobDetail, timelineEntry } from '../test/fixtures'
import type { JobDetail } from '../api/types'

/**
 * Job detail is the page that decides whether a human authorizes a change, so
 * these tests pin the two properties that would be most damaging to lose:
 *
 *   1. An invalidated approval is impossible to miss (spec section 74).
 *   2. An AI-only finding is visually and textually distinct from a verified
 *      fact, so a reviewer cannot mistake reasoning for evidence.
 */

function routeFetch(job: JobDetail): void {
  const fetchMock = vi.fn((input: unknown) => {
    const url = String(input)

    const respond = (body: unknown, status = 200): Promise<Response> =>
      Promise.resolve({
        ok: status >= 200 && status < 300,
        status,
        text: () => Promise.resolve(body === null ? '' : JSON.stringify(body)),
      } as unknown as Response)

    if (url.includes('/report')) return respond(null)
    if (url.includes('/timeline')) return respond({ items: [timelineEntry()] })
    if (url.includes('/artifacts')) return respond({ items: [artifact()] })
    // Sibling jobs for the sidebar panel.
    if (url.includes('/api/v1/jobs?')) return respond({ items: [], total: 0, limit: 5, offset: 0 })
    // The job itself. The detail route is a prefix of the sub-resource routes
    // above, so this must come last.
    if (url.includes('/api/v1/jobs/job-1')) return respond(job)
    return respond({ detail: `unexpected request: ${url}` }, 404)
  })
  globalThis.fetch = fetchMock as unknown as typeof fetch
}

function renderJobDetail() {
  return render(
    <MemoryRouter
      initialEntries={['/jobs/job-1']}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <Routes>
        <Route path="/jobs/:jobId" element={<JobDetailPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

const originalFetch = globalThis.fetch

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  globalThis.fetch = originalFetch
})

describe('JobDetailPage — invalidated approval warning', () => {
  const invalidated: JobDetail = jobDetail({
    status: 'READY_FOR_REVIEW',
    current_approval_valid: false,
    approvals: [
      {
        id: 'approval-1',
        decision: 'APPROVE',
        decided_by: 'ana@edrevel.ai',
        decided_at: '2024-05-01T11:00:00Z',
        comment: 'Looks good to me.',
        commit_sha: '9999999999999999999999999999999999999999',
        invalidated_at: '2024-05-01T11:30:00Z',
        invalidated_reason: 'New commits pushed after the approval',
        is_valid: false,
      },
    ],
  })

  it('renders an unmissable warning when an approval exists but no longer covers the commit', async () => {
    routeFetch(invalidated)
    renderJobDetail()

    const warning = await screen.findByTestId('invalidated-approval-warning')

    expect(warning).toBeInTheDocument()
    expect(warning).toHaveAttribute('role', 'alert')
    expect(within(warning).getByText(/previous approval was invalidated/i)).toBeInTheDocument()
    // It must name both commits: the one that was authorized and the one now under review.
    // (Each short SHA also appears inside the adjacent 12-character `<code>`, so match all.)
    expect(within(warning).getAllByText('999999999999').length).toBeGreaterThan(0)
    expect(within(warning).getAllByText('abcdef123456').length).toBeGreaterThan(0)
    expect(within(warning).getByText(/New commits pushed after the approval/i)).toBeInTheDocument()
  })

  it('does not render the warning when no approval has been recorded', async () => {
    routeFetch(jobDetail({ approvals: [], current_approval_valid: false }))
    renderJobDetail()

    // Wait for the page to finish loading before asserting an absence.
    await screen.findByRole('heading', { name: /Add order total validation/i })
    expect(screen.queryByTestId('invalidated-approval-warning')).not.toBeInTheDocument()
  })

  it('does not render the warning when the approval is current', async () => {
    routeFetch(
      jobDetail({
        current_approval_valid: true,
        approvals: [
          {
            id: 'approval-2',
            decision: 'APPROVE',
            decided_by: 'ana@edrevel.ai',
            decided_at: '2024-05-01T11:00:00Z',
            comment: '',
            commit_sha: 'abcdef1234567890abcdef1234567890abcdef12',
            invalidated_at: null,
            invalidated_reason: null,
            is_valid: true,
          },
        ],
      }),
    )
    renderJobDetail()

    await screen.findByRole('heading', { name: /Add order total validation/i })
    expect(screen.queryByTestId('invalidated-approval-warning')).not.toBeInTheDocument()
    expect(screen.getByText(/A valid approval covers the current commit/i)).toBeInTheDocument()
  })
})

describe('JobDetailPage — evidence is visible', () => {
  it('visually and textually distinguishes an AI-only finding from a verified fact', async () => {
    routeFetch(jobDetail())
    renderJobDetail()

    await screen.findByRole('heading', { name: /Add order total validation/i })

    const verified = await screen.findByText('Order total is not validated for negative values')
    const verifiedCard = verified.closest('article')
    expect(verifiedCard).not.toBeNull()
    expect(verifiedCard).toHaveAttribute('data-verified', 'true')
    expect(verifiedCard).toHaveAttribute('data-provenance', 'DETERMINISTIC')
    expect(within(verifiedCard as HTMLElement).getByText(/Verified fact/i)).toBeInTheDocument()

    const aiOnly = screen.getByText('Validation logic may be duplicated in the billing service')
    const aiCard = aiOnly.closest('article')
    expect(aiCard).not.toBeNull()
    expect(aiCard).toHaveAttribute('data-verified', 'false')
    expect(aiCard).toHaveAttribute('data-provenance', 'AI_ONLY')
    expect(
      within(aiCard as HTMLElement).getByText(/Not a verified fact — AI-suggested or unsupported/i),
    ).toBeInTheDocument()
    // The confidence ceiling was applied, and the UI must say so.
    expect(
      within(aiCard as HTMLElement).getByText(/Confidence capped by evidence strength/i),
    ).toBeInTheDocument()
    // No evidence cited forces human attention.
    expect(within(aiCard as HTMLElement).getByText(/Requires human review/i)).toBeInTheDocument()
    // The AI-only finding is not the same element as the verified one.
    expect(aiCard).not.toBe(verifiedCard)
  })

  it('shows evidence strength and provenance when a finding is expanded', async () => {
    routeFetch(jobDetail())
    renderJobDetail()

    const toggle = await screen.findByRole('button', {
      name: /Show evidence for Order total is not validated for negative values/i,
    })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(toggle)

    await waitFor(() => {
      expect(toggle).toHaveAttribute('aria-expanded', 'true')
    })

    // MACHINE strength is rendered as a labelled badge, not just a colour.
    expect(screen.getAllByText('MACHINE').length).toBeGreaterThan(0)
    expect(screen.getByText(/pytest exited 1 with two failures/i)).toBeInTheDocument()
    expect(screen.getByText(/Reject negative totals at the API boundary/i)).toBeInTheDocument()
  })
})
