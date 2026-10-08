import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, apiBaseUrl, parseJsonBody, request } from './client'

/**
 * These tests exist because the client is the one place where a mistake is both
 * invisible and expensive: a 404 rendered as "something went wrong" sends an
 * operator looking for a server fault that does not exist, and a request that
 * hangs forever leaves a spinner that never resolves on a 500.
 */

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(body === null ? '' : JSON.stringify(body)),
  } as unknown as Response
}

function emptyResponse(status = 204): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(''),
  } as unknown as Response
}

const originalFetch = globalThis.fetch

/**
 * The configured base URL. Tests assert against what the client actually
 * resolves rather than a hardcoded host, so the suite passes whether or not a
 * local `.env` overrides `VITE_SCOUT_API_URL`.
 */
const BASE = apiBaseUrl()

beforeEach(() => {
  vi.restoreAllMocks()
})

afterEach(() => {
  globalThis.fetch = originalFetch
})

describe('request', () => {
  it('resolves the parsed JSON body on success', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ status: 'ok', service: 'scout-api', version: '0.1.0' })))
    globalThis.fetch = fetchMock as unknown as typeof fetch

    const result = await request<{ status: string; service: string; version: string }>('/health')

    expect(result).toEqual({ status: 'ok', service: 'scout-api', version: '0.1.0' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${BASE}/health`)
    expect(init.method).toBe('GET')
  })

  it('resolves null for an empty body instead of throwing a JSON parse error', async () => {
    globalThis.fetch = vi.fn(() => Promise.resolve(emptyResponse())) as unknown as typeof fetch

    await expect(request<null>('/api/v1/jobs/job-1/report')).resolves.toBeNull()
  })

  it('raises ApiError with status 404 so the UI can say "not found"', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(jsonResponse({ detail: 'No verification job with that id' }, 404)),
    ) as unknown as typeof fetch

    const failure = await request('/api/v1/jobs/missing').catch((error: unknown) => error)

    expect(failure).toBeInstanceOf(ApiError)
    const apiError = failure as ApiError
    expect(apiError.status).toBe(404)
    expect(apiError.isNotFound).toBe(true)
    expect(apiError.serverMessage).toBe('No verification job with that id')
    expect(apiError.displayMessage).toContain('No verification job with that id')
  })

  it('raises a distinct ApiError for a 500 that is neither 404 nor unreachable', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(jsonResponse({ detail: 'database is on fire' }, 500)),
    ) as unknown as typeof fetch

    const failure = (await request('/api/v1/jobs').catch((error: unknown) => error)) as ApiError

    expect(failure.status).toBe(500)
    expect(failure.isNotFound).toBe(false)
    expect(failure.isUnreachable).toBe(false)
    expect(failure.statusText).toBe('Internal Server Error')
    expect(failure.displayMessage).toBe('500 Internal Server Error: database is on fire')
  })

  it('surfaces FastAPI validation errors whose detail is a list', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(
        jsonResponse({ detail: [{ msg: 'field required' }, { msg: 'not a valid decision' }] }, 422),
      ),
    ) as unknown as typeof fetch

    const failure = (await request('/api/v1/jobs/job-1/approve').catch((error: unknown) => error)) as ApiError

    expect(failure.status).toBe(422)
    expect(failure.serverMessage).toBe('field required; not a valid decision')
  })

  it('aborts a hanging request and reports a timeout rather than hanging forever', async () => {
    globalThis.fetch = vi.fn((_url: string, init?: RequestInit) => {
      return new Promise<Response>((_resolve, reject) => {
        const signal = init?.signal
        if (signal) {
          signal.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'))
          })
        }
      })
    }) as unknown as typeof fetch

    const started = Date.now()
    const failure = (await request('/api/v1/jobs', { timeoutMs: 25 }).catch(
      (error: unknown) => error,
    )) as ApiError

    expect(failure).toBeInstanceOf(ApiError)
    expect(failure.kind).toBe('timeout')
    expect(failure.status).toBe(0)
    expect(failure.isUnreachable).toBe(true)
    expect(failure.displayMessage).toContain('timed out')
    expect(Date.now() - started).toBeLessThan(2000)
  })

  it('reports a network failure as unreachable rather than as an HTTP status', async () => {
    globalThis.fetch = vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))) as unknown as typeof fetch

    const failure = (await request('/health').catch((error: unknown) => error)) as ApiError

    expect(failure.kind).toBe('network')
    expect(failure.status).toBe(0)
    expect(failure.isUnreachable).toBe(true)
    expect(failure.displayMessage).toContain('Cannot reach the Scout API')
  })

  it('omits undefined, null, and empty query parameters', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ items: [], total: 0, limit: 25, offset: 0 })))
    globalThis.fetch = fetchMock as unknown as typeof fetch

    await request('/api/v1/jobs', {
      query: { status: 'FAILED', repository: undefined, limit: 25, offset: 0, empty: '' },
    })

    const [url] = fetchMock.mock.calls[0] as unknown as [string]
    expect(url).toContain('status=FAILED')
    expect(url).toContain('limit=25')
    expect(url).toContain('offset=0')
    expect(url).not.toContain('repository')
    expect(url).not.toContain('empty')
  })

  it('sends a JSON body and content type for a POST', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ id: 'job-1' })))
    globalThis.fetch = fetchMock as unknown as typeof fetch

    await request('/api/v1/jobs/job-1/approve', {
      method: 'POST',
      body: { decision: 'APPROVE', decided_by: 'reviewer', comment: '' },
    })

    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(init.method).toBe('POST')
    expect(init.body).toBe(
      JSON.stringify({ decision: 'APPROVE', decided_by: 'reviewer', comment: '' }),
    )
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json')
  })
})

describe('parseJsonBody', () => {
  it('returns the decoded value for JSON', async () => {
    await expect(parseJsonBody(jsonResponse({ a: 1 }))).resolves.toEqual({ a: 1 })
  })

  it('returns null for a blank body', async () => {
    await expect(parseJsonBody(emptyResponse())).resolves.toBeNull()
  })

  it('returns the raw text when the body is not JSON', async () => {
    const response = { text: () => Promise.resolve('<html>nginx 502</html>') } as unknown as Response
    await expect(parseJsonBody(response)).resolves.toBe('<html>nginx 502</html>')
  })
})

describe('apiBaseUrl', () => {
  it('defaults to localhost when VITE_SCOUT_API_URL is not configured', () => {
    expect(apiBaseUrl()).toBe('http://localhost:8000')
  })
})

describe('endpoint wrappers', () => {
  it('builds the job detail path with the id URL-encoded', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ id: 'job 1' })))
    globalThis.fetch = fetchMock as unknown as typeof fetch

    await api.getJob('job 1')

    const [url] = fetchMock.mock.calls[0] as unknown as [string]
    expect(url).toBe(`${BASE}/api/v1/jobs/job%201`)
  })

  it('posts an approval to the approve endpoint', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ id: 'job-1' })))
    globalThis.fetch = fetchMock as unknown as typeof fetch

    await api.approveJob('job-1', { decision: 'REQUEST_CHANGES', decided_by: 'ana', comment: 'see notes' })

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe(`${BASE}/api/v1/jobs/job-1/approve`)
    expect(init.method).toBe('POST')
  })

  it('exposes a distinct error for a missing resource through a wrapper', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(jsonResponse({ detail: 'not found' }, 404)),
    ) as unknown as typeof fetch

    const failure = (await api.getJob('nope').catch((error: unknown) => error)) as ApiError
    expect(failure.isNotFound).toBe(true)
  })
})
