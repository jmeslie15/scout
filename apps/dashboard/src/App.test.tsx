import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'

/**
 * Routing smoke test.
 *
 * Every route in the brief must render without throwing and must show the
 * section it claims to show. This is the test that catches the class of bug a
 * type checker cannot: a page that compiles but crashes on first render because
 * a response shape is not what the component assumed.
 *
 * Every request is answered from a stub, so this proves the pages handle a
 * realistic payload, not that the API exists.
 */

const ROUTES: readonly { path: string; expect: RegExp }[] = [
  { path: '/dashboard', expect: /Ten most recent jobs/i },
  { path: '/jobs', expect: /Every verification job, newest first/i },
  { path: '/jobs/job-1', expect: /Human authorization/i },
  { path: '/repositories', expect: /Repositories Scout watches/i },
  // The stub returns no recent jobs, so the repository page renders its empty
  // state; assert on the filter-independent stat card instead.
  { path: '/repositories/edrevel/scout', expect: /Jobs recorded/i },
  { path: '/questions', expect: /Scout escalates rather than guessing/i },
  { path: '/knowledge', expect: /Engineering memory: answers and decisions Scout can cite/i },
  { path: '/agents', expect: /fixed set of reasoning agents/i },
  { path: '/tools', expect: /Deterministic checks executed inside the sandbox/i },
  { path: '/audit', expect: /Append-only record of privileged actions/i },
]

const READY = {
  status: 'ready',
  checks: {
    database: { ok: true, detail: null },
    redis: { ok: true, detail: null },
    artifacts: { ok: true, detail: null },
    llm: { ok: true, detail: null },
  },
}

const JOB_SUMMARY = {
  id: 'job-1',
  repository_full_name: 'edrevel/scout',
  pull_request_number: 42,
  pull_request_title: 'Add order total validation',
  pull_request_url: 'https://github.com/edrevel/scout/pull/42',
  head_sha: 'abcdef1234567890abcdef1234567890abcdef12',
  status: 'READY_FOR_REVIEW',
  risk_level: 'MEDIUM',
  recommendation: 'REQUIRES_HUMAN_REVIEW',
  verification_status: 'PASSED_WITH_WARNINGS',
  created_at: '2024-05-01T11:50:00Z',
  started_at: '2024-05-01T11:50:00Z',
  completed_at: null,
  finding_counts: { CRITICAL: 0, HIGH: 1, MEDIUM: 1, LOW: 0, INFO: 0 },
  open_question_count: 1,
}

function body(items: unknown): unknown {
  return items
}

function installFetchStub(): void {
  globalThis.fetch = vi.fn((input: unknown) => {
    const url = String(input)

    let payload: unknown = { items: [] }
    if (url.includes('/ready')) payload = READY
    else if (url.includes('/health')) payload = { status: 'ok', service: 'scout-api', version: '0.1.0' }
    else if (url.includes('/metrics/summary'))
      payload = {
        jobs_total: 4,
        jobs_failed: 1,
        jobs_by_status: { READY_FOR_REVIEW: 2, FAILED: 1, TESTING: 1 },
        findings_by_severity: { HIGH: 2, MEDIUM: 3 },
        human_escalations: 1,
        llm_tokens: 12345,
        llm_cost_usd: 0.42,
        avg_job_duration_seconds: 120,
        test_failures: 2,
      }
    else if (url.includes('/report')) payload = null
    else if (url.includes('/timeline')) payload = body({ items: [] })
    else if (url.includes('/artifacts')) payload = body({ items: [] })
    else if (url.includes('/api/v1/repositories/')) payload = { ...REPO, recent_jobs: [] }
    else if (url.includes('/api/v1/repositories')) payload = body({ items: [REPO] })
    else if (url.includes('/api/v1/jobs/job-1')) payload = JOB_DETAIL
    else if (url.includes('/api/v1/jobs')) payload = { items: [JOB_SUMMARY], total: 1, limit: 25, offset: 0 }
    else if (url.includes('/api/v1/questions')) payload = { items: [], total: 0, limit: 25, offset: 0 }
    else if (url.includes('/api/v1/knowledge')) payload = body({ items: [] })
    else if (url.includes('/api/v1/agents')) payload = body({ items: [] })
    else if (url.includes('/api/v1/tools')) payload = body({ items: [] })
    else if (url.includes('/api/v1/audit')) payload = { items: [], total: 0, limit: 25, offset: 0 }

    return Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify(payload)),
    } as unknown as Response)
  }) as unknown as typeof fetch
}

const REPO = {
  full_name: 'edrevel/scout',
  github_id: 1234,
  default_branch: 'main',
  is_active: true,
  job_count: 3,
  last_job_at: '2024-05-01T11:50:00Z',
}

const JOB_DETAIL = {
  ...JOB_SUMMARY,
  base_sha: '0000000000000000000000000000000000000000',
  pull_request_author: 'octocat',
  commits: {
    base_sha: '0000000000000000000000000000000000000000',
    head_sha: JOB_SUMMARY.head_sha,
    merge_base_sha: null,
  },
  attempt: 1,
  max_attempts: 3,
  failure_reason: null,
  cancel_reason: null,
  stages: [
    {
      stage: 'PREPARING',
      outcome: 'SUCCEEDED',
      attempt: 1,
      started_at: '2024-05-01T11:50:00Z',
      completed_at: '2024-05-01T11:51:00Z',
      duration_seconds: 60,
      error: null,
      skip_reason: null,
    },
  ],
  findings: [],
  questions: [],
  tool_runs: [],
  agent_runs: [],
  approvals: [],
  current_approval_valid: false,
}

const originalFetch = globalThis.fetch

beforeEach(() => {
  installFetchStub()
})

afterEach(() => {
  globalThis.fetch = originalFetch
})

describe('App routing', () => {
  it('redirects / to /dashboard', async () => {
    render(
      <MemoryRouter initialEntries={['/']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/Ten most recent jobs/i)).toBeInTheDocument()
  })

  for (const route of ROUTES) {
    it(`renders ${route.path}`, async () => {
      render(
        <MemoryRouter
          initialEntries={[route.path]}
          future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        >
          <App />
        </MemoryRouter>,
      )
      expect(await screen.findByText(route.expect)).toBeInTheDocument()
    })
  }

  it('renders a not-found page for an unknown route instead of a blank screen', async () => {
    render(
      <MemoryRouter
        initialEntries={['/nope']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <App />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/Page not found/i)).toBeInTheDocument()
    expect(screen.getByText('/nope')).toBeInTheDocument()
  })

  it('shows the shell navigation on every route', async () => {
    render(
      <MemoryRouter
        initialEntries={['/tools']}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <App />
      </MemoryRouter>,
    )
    const nav = await screen.findByRole('navigation', { name: 'Primary' })
    for (const label of ['Dashboard', 'Jobs', 'Repositories', 'Questions', 'Knowledge', 'Agents', 'Tools', 'Audit']) {
      expect(within(nav).getByRole('link', { name: label })).toBeInTheDocument()
    }
  })
})
