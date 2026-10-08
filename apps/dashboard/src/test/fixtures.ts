import type { AgentRun, Artifact, JobDetail, JobStage, TimelineEntry, ToolRun } from '../api/types'

/**
 * Test fixtures.
 *
 * These are typed against the API contract on purpose: if the wire types change,
 * the fixtures stop compiling, which is the cheapest possible way to notice that
 * the dashboard and the contract have drifted apart.
 */

export const ISO_NOW = '2024-05-01T12:00:00Z'
export const ISO_THREE_MINUTES_AGO = '2024-05-01T11:57:00Z'
export const ISO_TEN_MINUTES_AGO = '2024-05-01T11:50:00Z'

export function toolRun(overrides: Partial<ToolRun> = {}): ToolRun {
  return {
    id: 'tool-run-1',
    tool: 'pytest',
    version: '8.1.0',
    command: 'pytest -q tests/',
    exit_code: 1,
    status: 'failed',
    duration_seconds: 12.5,
    summary: { failed: 2, passed: 41 },
    narrative: 'Two tests failed in tests/test_orders.py.',
    infrastructure_failure: false,
    stdout_artifact_id: 'artifact-stdout-1',
    stderr_artifact_id: null,
    ...overrides,
  }
}

export function agentRun(overrides: Partial<AgentRun> = {}): AgentRun {
  return {
    id: 'agent-run-1',
    role: 'code_reviewer',
    status: 'completed',
    prompt_version: 'code_reviewer@3',
    provider: 'fake',
    model: 'fake-model',
    latency_seconds: 4.2,
    input_tokens: 1200,
    output_tokens: 300,
    estimated_cost_usd: 0.0012,
    error: null,
    ...overrides,
  }
}

export function stage(overrides: Partial<JobStage> = {}): JobStage {
  return {
    stage: 'PREPARING',
    outcome: 'SUCCEEDED',
    attempt: 1,
    started_at: ISO_TEN_MINUTES_AGO,
    completed_at: ISO_THREE_MINUTES_AGO,
    duration_seconds: 420,
    error: null,
    skip_reason: null,
    ...overrides,
  }
}

export function timelineEntry(overrides: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    id: 'timeline-1',
    at: ISO_THREE_MINUTES_AGO,
    kind: 'stage.completed',
    actor: 'orchestrator',
    actor_type: 'SYSTEM',
    summary: 'PREPARING succeeded',
    metadata: {},
    ...overrides,
  }
}

export function artifact(overrides: Partial<Artifact> = {}): Artifact {
  return {
    id: 'artifact-stdout-1',
    kind: 'TOOL_STDOUT',
    content_type: 'text/plain',
    size_bytes: 2048,
    sha256: 'a'.repeat(64),
    filename: 'pytest.stdout.txt',
    created_at: ISO_THREE_MINUTES_AGO,
    expires_at: null,
    ...overrides,
  }
}

/** A job awaiting review, with one verified and one AI-only finding. */
export function jobDetail(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
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
    created_at: ISO_TEN_MINUTES_AGO,
    started_at: ISO_TEN_MINUTES_AGO,
    completed_at: null,
    finding_counts: { CRITICAL: 0, HIGH: 1, MEDIUM: 1, LOW: 0, INFO: 0 },
    open_question_count: 0,
    base_sha: '0000000000000000000000000000000000000000',
    pull_request_author: 'octocat',
    commits: {
      base_sha: '0000000000000000000000000000000000000000',
      head_sha: 'abcdef1234567890abcdef1234567890abcdef12',
      merge_base_sha: '1111111111111111111111111111111111111111',
    },
    attempt: 1,
    max_attempts: 3,
    failure_reason: null,
    cancel_reason: null,
    stages: [
      stage({ stage: 'PREPARING', outcome: 'SUCCEEDED' }),
      stage({ stage: 'ANALYZING', outcome: 'SUCCEEDED' }),
      stage({ stage: 'SECURITY_SCANNING', outcome: 'SKIPPED', skip_reason: 'No dependency files changed.' }),
    ],
    findings: [
      {
        id: 'finding-verified',
        severity: 'HIGH',
        category: 'TESTS',
        title: 'Order total is not validated for negative values',
        description: 'tests/test_orders.py::test_negative_total fails with exit code 1.',
        file: 'tests/test_orders.py',
        line_start: 18,
        line_end: 24,
        recommendation: 'Reject negative totals at the API boundary.',
        confidence: 1,
        confidence_capped: false,
        requires_human: false,
        origin: 'TOOL',
        source: 'pytest',
        provenance: 'DETERMINISTIC',
        is_verified_fact: true,
        evidence: [
          {
            kind: 'test_run',
            reference_id: 'tool-run-1',
            summary: 'pytest exited 1 with two failures',
            file_path: 'tests/test_orders.py',
            line_start: 18,
            line_end: 24,
            excerpt: 'E   AssertionError: -5.0 != 0.0',
            artifact_id: 'artifact-stdout-1',
            url: null,
            strength: 'MACHINE',
          },
        ],
        created_at: ISO_THREE_MINUTES_AGO,
      },
      {
        id: 'finding-ai-only',
        severity: 'MEDIUM',
        category: 'MAINTAINABILITY',
        title: 'Validation logic may be duplicated in the billing service',
        description: 'The reviewer agent suspects a second copy of this rule exists elsewhere.',
        file: null,
        line_start: null,
        line_end: null,
        recommendation: 'Search the billing service for a duplicate rule before merging.',
        confidence: 0.65,
        confidence_capped: true,
        requires_human: true,
        origin: 'AGENT',
        source: 'code_reviewer',
        provenance: 'AI_ONLY',
        is_verified_fact: false,
        evidence: [
          {
            kind: 'agent_run',
            reference_id: 'agent-run-1',
            summary: 'Code reviewer inferred duplication from naming alone',
            file_path: null,
            line_start: null,
            line_end: null,
            excerpt: null,
            artifact_id: null,
            url: null,
            strength: 'INFERENCE',
          },
        ],
        created_at: ISO_THREE_MINUTES_AGO,
      },
    ],
    questions: [],
    tool_runs: [toolRun()],
    agent_runs: [agentRun()],
    approvals: [],
    current_approval_valid: false,
    ...overrides,
  }
}
