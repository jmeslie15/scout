/**
 * Typed fetch client for the Scout control-plane API.
 *
 * Design notes:
 *
 * - One `request<T>()` helper does all transport work: URL building, JSON
 *   parsing, timeouts, and error normalisation. Every endpoint function is a
 *   thin, fully typed wrapper over it.
 * - Failures become `ApiError`, which carries the HTTP status, the parsed error
 *   body (when the server sent one), and the request path. Callers can
 *   distinguish "not found" (404) from "something went wrong" (5xx) without
 *   string-matching a message.
 * - Timeouts use `AbortController` and surface as `ApiError` with status `0` and
 *   `kind: 'timeout'`, so a hung request can never leave a spinner spinning.
 * - An empty body is legitimate in several places (`/report` returns `null` or
 *   nothing, POSTs may return 204), so JSON parsing is tolerant: blank text
 *   becomes `null` rather than a `SyntaxError`.
 * - The base URL comes from `VITE_SCOUT_API_URL` with a localhost default. It is
 *   the only place a host is named, per the project's configuration policy.
 */

import type {
  AgentSummary,
  AnswerQuestionRequest,
  ApprovalRequest,
  Artifact,
  AuditEvent,
  AuditListParams,
  HealthResponse,
  ItemsResponse,
  JobDetail,
  JobListParams,
  JobSummary,
  KnowledgeDocument,
  KnowledgeDocumentRequest,
  KnowledgeListParams,
  MetricsSummary,
  Paginated,
  Question,
  QuestionListParams,
  ReadinessResponse,
  ReasonRequest,
  Report,
  RepositoryDetail,
  RepositorySummary,
  TimelineEntry,
  ToolSummary,
} from './types'

/* ------------------------------------------------------------------ config -- */

const DEFAULT_BASE_URL = 'http://localhost:8000'
const DEFAULT_TIMEOUT_MS = 15_000

/**
 * The API origin. `VITE_SCOUT_API_URL` is the single configuration point; the
 * localhost fallback exists so a fresh clone runs against a local `make api`
 * with no `.env` file.
 */
export function apiBaseUrl(): string {
  const configured: unknown = import.meta.env.VITE_SCOUT_API_URL
  if (typeof configured === 'string' && configured.trim() !== '') {
    return configured.replace(/\/+$/, '')
  }
  return DEFAULT_BASE_URL
}

/** Where the API docs / raw endpoint live, for links in error states. */
export function apiHealthUrl(): string {
  return `${apiBaseUrl()}/health`
}

/* ------------------------------------------------------------------- error -- */

export type ApiErrorKind = 'http' | 'timeout' | 'network' | 'parse'

export interface RequestOptions {
  /** Query parameters. `undefined` and `null` values are omitted. */
  query?: Record<string, string | number | boolean | undefined | null>
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  timeoutMs?: number
  signal?: AbortSignal
}

/** A failed API call. Never thrown for a successful (2xx) response. */
export class ApiError extends Error {
  readonly status: number
  readonly kind: ApiErrorKind
  readonly body: unknown
  readonly path: string

  constructor(args: {
    message: string
    status: number
    kind: ApiErrorKind
    body: unknown
    path: string
  }) {
    super(args.message)
    this.name = 'ApiError'
    this.status = args.status
    this.kind = args.kind
    this.body = args.body
    this.path = args.path
  }

  /** True for HTTP 404. The UI can say "not found" instead of "failed". */
  get isNotFound(): boolean {
    return this.status === 404
  }

  /** True when the request never reached a working server, or timed out. */
  get isUnreachable(): boolean {
    return this.kind === 'network' || this.kind === 'timeout'
  }

  /**
   * Best-effort human message. FastAPI's default error body is
   * `{"detail": "..."}` where `detail` may itself be a list of validation
   * errors; both shapes are handled.
   */
  get serverMessage(): string | null {
    const body = this.body
    if (typeof body === 'string' && body.trim() !== '') return body
    if (typeof body !== 'object' || body === null) return null
    const detail: unknown = (body as Record<string, unknown>)['detail']
    if (typeof detail === 'string' && detail.trim() !== '') return detail
    if (Array.isArray(detail)) {
      const messages = detail
        .map((entry) =>
          typeof entry === 'object' && entry !== null
            ? (entry as Record<string, unknown>)['msg']
            : null,
        )
        .filter((msg): msg is string => typeof msg === 'string')
      if (messages.length > 0) return messages.join('; ')
    }
    const message: unknown = (body as Record<string, unknown>)['message']
    if (typeof message === 'string' && message.trim() !== '') return message
    return null
  }

  /** A single line suitable for `ErrorState` / a banner. */
  get displayMessage(): string {
    const server = this.serverMessage
    if (this.kind === 'timeout') {
      return `Request to ${this.path} timed out after the client timeout.`
    }
    if (this.kind === 'network') {
      return `Cannot reach the Scout API at ${apiBaseUrl()}. Is the API running?`
    }
    if (this.status === 404) {
      return server ?? `Not found: ${this.path}`
    }
    if (server !== null) {
      return `${this.status} ${this.statusText}: ${server}`
    }
    return `${this.status} ${this.statusText}`
  }

  get statusText(): string {
    switch (this.status) {
      case 400:
        return 'Bad Request'
      case 401:
        return 'Unauthorized'
      case 403:
        return 'Forbidden'
      case 404:
        return 'Not Found'
      case 409:
        return 'Conflict'
      case 422:
        return 'Unprocessable Entity'
      case 429:
        return 'Too Many Requests'
      case 500:
        return 'Internal Server Error'
      case 502:
        return 'Bad Gateway'
      case 503:
        return 'Service Unavailable'
      case 504:
        return 'Gateway Timeout'
      default:
        return this.status === 0 ? 'No Response' : 'Error'
    }
  }
}

/** Convert anything thrown by `fetch` into an `ApiError`. */
function toApiError(error: unknown, path: string, timedOut: boolean): ApiError {
  if (error instanceof ApiError) return error
  if (timedOut) {
    return new ApiError({
      message: `Request timed out: ${path}`,
      status: 0,
      kind: 'timeout',
      body: null,
      path,
    })
  }
  if (error instanceof DOMException && error.name === 'AbortError') {
    return new ApiError({
      message: `Request aborted: ${path}`,
      status: 0,
      kind: 'timeout',
      body: null,
      path,
    })
  }
  const message = error instanceof Error ? error.message : String(error)
  return new ApiError({
    message: `Network error: ${message}`,
    status: 0,
    kind: 'network',
    body: null,
    path,
  })
}

/* ----------------------------------------------------------------- request -- */

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = new URL(path.startsWith('/') ? path : `/${path}`, `${apiBaseUrl()}/`)
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null || value === '') continue
      url.searchParams.set(key, String(value))
    }
  }
  return url.toString()
}

/**
 * Parse a response body as JSON, tolerating an empty body.
 * Returns `null` for 204 and for a zero-length body.
 */
export async function parseJsonBody(response: Response): Promise<unknown> {
  const text = await response.text()
  if (text.trim() === '') return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    // Not JSON. Hand it back as text so error rendering can still show it.
    return text
  }
}

/**
 * Perform one API request and decode the JSON response as `T`.
 *
 * @throws {ApiError} for a non-2xx status, a timeout, a network failure, or a
 *   body that cannot be parsed as JSON. A successful response with an empty
 *   body resolves to `null` cast to `T` — callers that can receive an empty
 *   body declare `T` accordingly (for example `Report | null`).
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS, signal } = options
  const url = buildUrl(path, options.query)

  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)

  // Propagate a caller-supplied abort as well as our timeout.
  const onExternalAbort = (): void => controller.abort()
  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', onExternalAbort)
  }

  let response: Response
  try {
    const init: RequestInit = {
      method,
      headers: { Accept: 'application/json' },
      signal: controller.signal,
    }
    if (body !== undefined) {
      init.headers = {
        ...(init.headers as Record<string, string>),
        'Content-Type': 'application/json',
      }
      init.body = JSON.stringify(body)
    }
    response = await fetch(url, init)
  } catch (error) {
    throw toApiError(error, path, timedOut)
  } finally {
    clearTimeout(timer)
    if (signal) signal.removeEventListener('abort', onExternalAbort)
  }

  const parsed = await parseJsonBody(response)

  if (!response.ok) {
    throw new ApiError({
      message: `HTTP ${response.status} for ${path}`,
      status: response.status,
      kind: 'http',
      body: parsed,
      path,
    })
  }

  return parsed as T
}

/* --------------------------------------------------------------- endpoints -- */

export const api = {
  health: (options?: RequestOptions): Promise<HealthResponse> =>
    request<HealthResponse>('/health', options),

  ready: (options?: RequestOptions): Promise<ReadinessResponse> =>
    request<ReadinessResponse>('/ready', options),

  listJobs: (params: JobListParams = {}, options?: RequestOptions): Promise<Paginated<JobSummary>> =>
    request<Paginated<JobSummary>>('/api/v1/jobs', { ...options, query: { ...params } }),

  getJob: (jobId: string, options?: RequestOptions): Promise<JobDetail> =>
    request<JobDetail>(`/api/v1/jobs/${encodeURIComponent(jobId)}`, options),

  getReport: (jobId: string, options?: RequestOptions): Promise<Report | null> =>
    request<Report | null>(`/api/v1/jobs/${encodeURIComponent(jobId)}/report`, options),

  getTimeline: (jobId: string, options?: RequestOptions): Promise<ItemsResponse<TimelineEntry>> =>
    request<ItemsResponse<TimelineEntry>>(
      `/api/v1/jobs/${encodeURIComponent(jobId)}/timeline`,
      options,
    ),

  listArtifacts: (jobId: string, options?: RequestOptions): Promise<ItemsResponse<Artifact>> =>
    request<ItemsResponse<Artifact>>(
      `/api/v1/jobs/${encodeURIComponent(jobId)}/artifacts`,
      options,
    ),

  approveJob: (
    jobId: string,
    payload: ApprovalRequest,
    options?: RequestOptions,
  ): Promise<JobDetail> =>
    request<JobDetail>(`/api/v1/jobs/${encodeURIComponent(jobId)}/approve`, {
      ...options,
      method: 'POST',
      body: payload,
    }),

  cancelJob: (jobId: string, payload: ReasonRequest, options?: RequestOptions): Promise<JobDetail> =>
    request<JobDetail>(`/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`, {
      ...options,
      method: 'POST',
      body: payload,
    }),

  retryJob: (jobId: string, payload: ReasonRequest, options?: RequestOptions): Promise<JobDetail> =>
    request<JobDetail>(`/api/v1/jobs/${encodeURIComponent(jobId)}/retry`, {
      ...options,
      method: 'POST',
      body: payload,
    }),

  listQuestions: (
    params: QuestionListParams = {},
    options?: RequestOptions,
  ): Promise<Paginated<Question>> =>
    request<Paginated<Question>>('/api/v1/questions', { ...options, query: { ...params } }),

  answerQuestion: (
    questionId: string,
    payload: AnswerQuestionRequest,
    options?: RequestOptions,
  ): Promise<Question> =>
    request<Question>(`/api/v1/questions/${encodeURIComponent(questionId)}/answer`, {
      ...options,
      method: 'POST',
      body: payload,
    }),

  listRepositories: (options?: RequestOptions): Promise<ItemsResponse<RepositorySummary>> =>
    request<ItemsResponse<RepositorySummary>>('/api/v1/repositories', options),

  getRepository: (
    owner: string,
    name: string,
    options?: RequestOptions,
  ): Promise<RepositoryDetail> =>
    request<RepositoryDetail>(
      `/api/v1/repositories/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`,
      options,
    ),

  listKnowledge: (
    params: KnowledgeListParams = {},
    options?: RequestOptions,
  ): Promise<ItemsResponse<KnowledgeDocument>> =>
    request<ItemsResponse<KnowledgeDocument>>('/api/v1/knowledge', {
      ...options,
      query: { ...params },
    }),

  createKnowledge: (
    payload: KnowledgeDocumentRequest,
    options?: RequestOptions,
  ): Promise<KnowledgeDocument> =>
    request<KnowledgeDocument>('/api/v1/knowledge', {
      ...options,
      method: 'POST',
      body: payload,
    }),

  listAgents: (options?: RequestOptions): Promise<ItemsResponse<AgentSummary>> =>
    request<ItemsResponse<AgentSummary>>('/api/v1/agents', options),

  listTools: (options?: RequestOptions): Promise<ItemsResponse<ToolSummary>> =>
    request<ItemsResponse<ToolSummary>>('/api/v1/tools', options),

  listAudit: (params: AuditListParams = {}, options?: RequestOptions): Promise<Paginated<AuditEvent>> =>
    request<Paginated<AuditEvent>>('/api/v1/audit', { ...options, query: { ...params } }),

  metricsSummary: (options?: RequestOptions): Promise<MetricsSummary> =>
    request<MetricsSummary>('/api/v1/metrics/summary', options),
}

export type ScoutApi = typeof api
