/**
 * Wire types for the Scout control-plane API.
 *
 * These mirror the API contract exactly. The backend is written by a separate
 * workstream, so this file is the dashboard's only source of truth about
 * response shapes and it must not invent fields. Enum-valued fields are typed
 * as string unions taken verbatim from `packages/domain/scout_domain/enums.py`,
 * except status-like fields where the server is allowed to introduce a value
 * this build does not know about — those are widened to `string` and handled by
 * the badge helpers in `src/lib/badges.ts`, which fall back safely rather than
 * rendering nothing.
 */

/* ------------------------------------------------------------------ enums -- */

export type JobStatus =
  | 'RECEIVED'
  | 'QUEUED'
  | 'PREPARING'
  | 'ANALYZING'
  | 'REVIEWING'
  | 'SECURITY_SCANNING'
  | 'TESTING'
  | 'FEATURE_VERIFYING'
  | 'INVESTIGATING'
  | 'WAITING_FOR_HUMAN'
  | 'READY_FOR_REVIEW'
  | 'FAILED'
  | 'CANCELLED'
  | 'COMPLETED'

/** In-flight statuses: the orchestrator is expected to be doing work. */
export const ACTIVE_JOB_STATUSES: readonly JobStatus[] = [
  'RECEIVED',
  'QUEUED',
  'PREPARING',
  'ANALYZING',
  'REVIEWING',
  'SECURITY_SCANNING',
  'TESTING',
  'FEATURE_VERIFYING',
  'INVESTIGATING',
]

/** Statuses that require a human decision before the job can finish. */
export const HUMAN_GATE_JOB_STATUSES: readonly JobStatus[] = [
  'WAITING_FOR_HUMAN',
  'READY_FOR_REVIEW',
]

/** Terminal statuses: no automatic transition may follow. */
export const TERMINAL_JOB_STATUSES: readonly JobStatus[] = ['COMPLETED', 'CANCELLED']

export const ALL_JOB_STATUSES: readonly JobStatus[] = [
  'RECEIVED',
  'QUEUED',
  'PREPARING',
  'ANALYZING',
  'REVIEWING',
  'SECURITY_SCANNING',
  'TESTING',
  'FEATURE_VERIFYING',
  'INVESTIGATING',
  'WAITING_FOR_HUMAN',
  'READY_FOR_REVIEW',
  'FAILED',
  'CANCELLED',
  'COMPLETED',
]

export type Severity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | 'INFO'
export const ALL_SEVERITIES: readonly Severity[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO']

export type RiskLevel = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'
export const ALL_RISK_LEVELS: readonly RiskLevel[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']

export type Provenance = 'DETERMINISTIC' | 'AI_ASSISTED' | 'AI_ONLY' | 'UNSUPPORTED'
export const ALL_PROVENANCE: readonly Provenance[] = [
  'DETERMINISTIC',
  'AI_ASSISTED',
  'AI_ONLY',
  'UNSUPPORTED',
]

export type EvidenceStrength = 'MACHINE' | 'INSPECTION' | 'AUTHORITATIVE' | 'INFERENCE'
export type EvidenceKind =
  | 'tool_run'
  | 'test_run'
  | 'file_range'
  | 'git_diff'
  | 'repository_inspection'
  | 'github_check'
  | 'knowledge_document'
  | 'human_answer'
  | 'agent_run'
  | 'artifact'

export type VerificationStatus =
  | 'PENDING'
  | 'PASSED'
  | 'PASSED_WITH_WARNINGS'
  | 'FAILED'
  | 'INCONCLUSIVE'

export type Recommendation =
  | 'PASS'
  | 'PASS_WITH_WARNINGS'
  | 'REQUIRES_HUMAN_REVIEW'
  | 'REQUEST_CHANGES'
  | 'FAIL'

export type ApprovalDecision = 'APPROVE' | 'REQUEST_CHANGES' | 'REJECT'
export const ALL_APPROVAL_DECISIONS: readonly ApprovalDecision[] = [
  'APPROVE',
  'REQUEST_CHANGES',
  'REJECT',
]

export type ActorType = 'SYSTEM' | 'AGENT' | 'HUMAN' | 'WEBHOOK' | 'WORKER'

export type QuestionStatus = 'OPEN' | 'ANSWERED' | 'DISMISSED' | 'SUPERSEDED'
export const ALL_QUESTION_STATUSES: readonly QuestionStatus[] = [
  'OPEN',
  'ANSWERED',
  'DISMISSED',
  'SUPERSEDED',
]

export type StageOutcome =
  | 'PENDING'
  | 'RUNNING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'SKIPPED'
  | 'CANCELLED'

export type ToolRunStatus =
  | 'passed'
  | 'failed'
  | 'error'
  | 'timeout'
  | 'skipped'
  | 'not_applicable'

export type CriterionStatus = 'passed' | 'failed' | 'blocked' | 'not_verified'

/* ------------------------------------------------------------------ models -- */

export interface EvidenceReference {
  kind: EvidenceKind
  reference_id: string
  summary: string
  file_path: string | null
  line_start: number | null
  line_end: number | null
  excerpt: string | null
  artifact_id: string | null
  url: string | null
  strength: EvidenceStrength
}

export interface FindingCounts {
  CRITICAL: number
  HIGH: number
  MEDIUM: number
  LOW: number
  INFO: number
}

export interface Finding {
  id: string
  severity: Severity
  category: string
  title: string
  description: string
  file: string | null
  line_start: number | null
  line_end: number | null
  recommendation: string
  confidence: number
  confidence_capped: boolean
  requires_human: boolean
  origin: 'TOOL' | 'AGENT'
  source: string
  provenance: Provenance
  is_verified_fact: boolean
  evidence: EvidenceReference[]
  created_at: string
}

export interface QuestionAnswer {
  id: string
  body: string
  answered_by: string
  answered_at: string
}

export interface Question {
  id: string
  job_id: string
  title: string
  description: string
  context: string
  investigation_summary: string
  severity: Severity
  options: string[]
  default_assumption: string | null
  status: QuestionStatus
  created_by: string
  created_at: string
  evidence: EvidenceReference[]
  answers: QuestionAnswer[]
}

export interface ToolRun {
  id: string
  tool: string
  version: string | null
  command: string
  exit_code: number | null
  status: ToolRunStatus
  duration_seconds: number
  summary: Record<string, unknown>
  narrative: string
  infrastructure_failure: boolean
  stdout_artifact_id: string | null
  stderr_artifact_id: string | null
}

export interface AgentRun {
  id: string
  role: string
  status: string
  prompt_version: string
  provider: string | null
  model: string | null
  latency_seconds: number | null
  input_tokens: number
  output_tokens: number
  estimated_cost_usd: number
  error: string | null
}

export interface Approval {
  id: string
  decision: ApprovalDecision
  decided_by: string
  decided_at: string
  comment: string
  commit_sha: string
  invalidated_at: string | null
  invalidated_reason: string | null
  is_valid: boolean
}

export interface Artifact {
  id: string
  kind: string
  content_type: string
  size_bytes: number
  sha256: string
  filename: string | null
  created_at: string
  expires_at: string | null
}

export interface JobStage {
  stage: string
  outcome: StageOutcome
  attempt: number
  started_at: string | null
  completed_at: string | null
  duration_seconds: number | null
  error: string | null
  skip_reason: string | null
}

export interface Commits {
  base_sha: string
  head_sha: string
  merge_base_sha: string | null
}

export interface JobSummary {
  id: string
  repository_full_name: string
  pull_request_number: number
  pull_request_title: string
  pull_request_url: string
  head_sha: string
  status: JobStatus | string
  risk_level: RiskLevel | string | null
  recommendation: string | null
  verification_status: VerificationStatus | string
  created_at: string
  started_at: string | null
  completed_at: string | null
  finding_counts: FindingCounts
  open_question_count: number
}

export interface JobDetail extends JobSummary {
  base_sha: string
  pull_request_author: string
  commits: Commits
  attempt: number
  max_attempts: number
  failure_reason: string | null
  cancel_reason: string | null
  stages: JobStage[]
  findings: Finding[]
  questions: Question[]
  tool_runs: ToolRun[]
  agent_runs: AgentRun[]
  approvals: Approval[]
  current_approval_valid: boolean
}

export interface TimelineEntry {
  id: string
  at: string
  kind: string
  actor: string
  actor_type: ActorType
  summary: string
  metadata: Record<string, unknown>
}

export interface AuditEvent {
  id: string
  at: string
  actor: string
  actor_type: ActorType
  action: string
  resource: string
  job_id: string | null
  metadata: Record<string, unknown>
}

export interface RepositorySummary {
  full_name: string
  github_id: number | null
  default_branch: string
  is_active: boolean
  job_count: number
  last_job_at: string | null
}

export interface RepositoryDetail extends RepositorySummary {
  recent_jobs: JobSummary[]
}

export interface KnowledgeDocument {
  id: string
  title: string
  body: string
  source_type: string
  authority: string
  scope: string
  tags: string[]
  created_at: string
  updated_at: string
  superseded_by_id: string | null
}

export interface AgentSummary {
  role: string
  description: string
  capabilities: string[]
  prompt_versions: string[]
  runs: number
  avg_latency_seconds: number | null
  totals: {
    input_tokens: number
    output_tokens: number
    estimated_cost_usd: number
  }
}

export interface ToolSummary {
  name: string
  category: string
  description: string
  capability: string
  applicable: boolean
  runs: number
  failures: number
  avg_duration_seconds: number | null
}

export interface HealthResponse {
  status: string
  service: string
  version: string
}

export interface ReadinessCheck {
  ok: boolean
  detail: string | null
}

export interface ReadinessResponse {
  status: 'ready' | 'degraded' | string
  checks: {
    database: ReadinessCheck
    redis: ReadinessCheck
    artifacts: ReadinessCheck
    llm: ReadinessCheck
  }
}

export interface MetricsSummary {
  jobs_total: number
  jobs_failed: number
  jobs_by_status: Record<string, number>
  findings_by_severity: Record<string, number>
  human_escalations: number
  llm_tokens: number
  llm_cost_usd: number
  avg_job_duration_seconds: number | null
  test_failures: number
}

/* ------------------------------------------------------------- envelopes -- */

export interface Paginated<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface ItemsResponse<T> {
  items: T[]
}

/**
 * The verification report.
 *
 * NOTE (contract gap): the API contract given to this workstream lists
 * `GET /api/v1/jobs/{id}/report -> Report | null` without an explicit `Report`
 * shape. The fields below are the subset the architecture document guarantees
 * (section 5, spec sections 23/44): a gated recommendation, enumerated findings
 * and criteria, unverified areas, and open questions. Every field is optional
 * and the report panel renders defensively, so a server that returns a
 * different or smaller shape degrades to "not available" rather than crashing.
 * See the "API contract gaps" section of apps/dashboard/README.md.
 */
export interface ReportCriterion {
  criterion?: string
  procedure?: string
  expected_result?: string
  actual_result?: string | null
  status?: CriterionStatus | string
  evidence?: EvidenceReference[]
}

export interface Report {
  id?: string
  job_id?: string
  recommendation?: string | null
  verification_status?: string | null
  summary?: string | null
  risk_level?: string | null
  findings?: Finding[]
  criteria?: ReportCriterion[]
  unverified_areas?: unknown
  open_questions?: Question[]
  evidence_backed_finding_count?: number
  generated_at?: string | null
  [key: string]: unknown
}

/* --------------------------------------------------------------- requests -- */

export interface JobListParams {
  status?: string
  repository?: string
  limit?: number
  offset?: number
}

export interface QuestionListParams {
  status?: string
  repository?: string
  limit?: number
  offset?: number
}

export interface AuditListParams {
  job_id?: string
  resource?: string
  limit?: number
  offset?: number
}

export interface KnowledgeListParams {
  scope?: string
  query?: string
  limit?: number
}

export interface ApprovalRequest {
  decision: ApprovalDecision
  decided_by: string
  comment: string
}

export interface AnswerQuestionRequest {
  body: string
  answered_by: string
  promote_to_knowledge: boolean
  knowledge_scope: string | null
}

export interface ReasonRequest {
  reason: string
}

export interface KnowledgeDocumentRequest {
  title: string
  body: string
  source_type: string
  authority: string
  scope: string
  tags: string[]
}
