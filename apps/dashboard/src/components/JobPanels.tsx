import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import type {
  AgentRun,
  Artifact,
  Finding,
  JobDetail,
  Question,
  StageOutcome,
  TimelineEntry,
  ToolRun,
} from '../api/types'
import {
  actorTypeMeta,
  formatBytes,
  formatCompactNumber,
  formatDuration,
  formatUsd,
  humanizeEnum,
  stageOutcomeMeta,
} from '../lib/badges'
import { AsyncBoundary } from './AsyncBoundary'
import type { AsyncState } from '../api/hooks'
import { Badge } from './Badge'
import { EmptyState } from './EmptyState'
import { EvidenceList } from './EvidenceList'
import { FindingCard } from './FindingCard'
import { StatCard } from './Layout'
import { RelativeTime } from './RelativeTime'
import { SeverityBadge } from './SeverityBadge'
import { Table, TBody, TD, TH, THead, TR } from './Table'

/* --------------------------------------------------------------- tool runs -- */

function toolStatusTone(status: ToolRun['status']): 'verified' | 'failed' | 'human' | 'neutral' {
  switch (status) {
    case 'passed':
      return 'verified'
    case 'failed':
    case 'error':
    case 'timeout':
      return 'failed'
    case 'skipped':
    case 'not_applicable':
      return 'neutral'
    default:
      return 'human'
  }
}

/** Deterministic tool executions. The strongest evidence Scout can hold. */
export function ToolRunsPanel({ toolRuns }: { toolRuns: readonly ToolRun[] }) {
  if (toolRuns.length === 0) {
    return (
      <EmptyState
        title="No tool runs recorded"
        description="No deterministic tool has executed for this job yet."
      />
    )
  }
  return (
    <div className="stack">
      {toolRuns.map((run) => (
        <article key={run.id} className="tool-run">
          <header className="tool-run__head">
            <code className="tool-run__tool">{run.tool}</code>
            {run.version !== null ? <span className="muted">v{run.version}</span> : null}
            <Badge tone={toolStatusTone(run.status)} description={`Tool status ${run.status}.`}>
              {run.status.replace(/_/g, ' ').toUpperCase()}
            </Badge>
            {run.infrastructure_failure ? (
              <span className="marker marker--capped" title="The tool failed for infrastructure reasons, not because the repository is broken.">
                Infrastructure failure — not a code finding
              </span>
            ) : null}
            <span className="muted">{formatDuration(run.duration_seconds)}</span>
            <span className="muted">
              exit code {run.exit_code === null ? '—' : run.exit_code}
            </span>
          </header>
          <p className="tool-run__command">
            <code>{run.command}</code>
          </p>
          {run.narrative !== '' ? <p className="tool-run__narrative">{run.narrative}</p> : null}
          {Object.keys(run.summary).length > 0 ? (
            <dl className="key-values">
              {Object.entries(run.summary).map(([key, value]) => (
                <div key={key} className="key-values__row">
                  <dt>{key}</dt>
                  <dd>
                    {typeof value === 'object' && value !== null
                      ? JSON.stringify(value)
                      : String(value)}
                  </dd>
                </div>
              ))}
            </dl>
          ) : null}
          {run.stdout_artifact_id !== null || run.stderr_artifact_id !== null ? (
            <p className="tool-run__artifacts muted">
              {run.stdout_artifact_id !== null ? (
                <>
                  stdout artifact <code>{run.stdout_artifact_id}</code>
                </>
              ) : null}
              {run.stdout_artifact_id !== null && run.stderr_artifact_id !== null ? ' · ' : null}
              {run.stderr_artifact_id !== null ? (
                <>
                  stderr artifact <code>{run.stderr_artifact_id}</code>
                </>
              ) : null}
            </p>
          ) : null}
        </article>
      ))}
    </div>
  )
}

/* -------------------------------------------------------------- agent runs -- */

/** AI reasoning runs. Never evidence on their own. */
export function AgentRunsPanel({ agentRuns }: { agentRuns: readonly AgentRun[] }) {
  if (agentRuns.length === 0) {
    return <EmptyState title="No agent runs recorded" description="No AI agent has run for this job." />
  }
  const totalCost = agentRuns.reduce((sum, run) => sum + run.estimated_cost_usd, 0)
  const totalTokens = agentRuns.reduce((sum, run) => sum + run.input_tokens + run.output_tokens, 0)

  return (
    <div className="stack">
      <div className="stat-grid stat-grid--compact">
        <StatCard label="Agent runs" value={agentRuns.length} />
        <StatCard label="Tokens" value={formatCompactNumber(totalTokens)} />
        <StatCard label="Estimated cost" value={formatUsd(totalCost)} />
        <StatCard
          label="Failures"
          value={agentRuns.filter((run) => run.status === 'failed').length}
          tone={agentRuns.some((run) => run.status === 'failed') ? 'bad' : 'good'}
        />
      </div>
      <Table caption="Agent runs for this job">
        <THead>
          <TR>
            <TH>Role</TH>
            <TH>Status</TH>
            <TH>Prompt version</TH>
            <TH>Provider / model</TH>
            <TH numeric>Latency</TH>
            <TH numeric>Input tokens</TH>
            <TH numeric>Output tokens</TH>
            <TH numeric>Cost</TH>
            <TH>Error</TH>
          </TR>
        </THead>
        <TBody>
          {agentRuns.map((run) => (
            <TR key={run.id}>
              <TD>
                <code>{run.role}</code>
              </TD>
              <TD>
                <Badge tone="ai" description="This is AI reasoning; on its own it is not verified evidence.">
                  {run.status.replace(/_/g, ' ').toUpperCase()}
                </Badge>
              </TD>
              <TD>
                <code>{run.prompt_version}</code>
              </TD>
              <TD>
                {run.provider ?? '—'}
                {run.model !== null ? ` / ${run.model}` : ''}
              </TD>
              <TD numeric>{formatDuration(run.latency_seconds)}</TD>
              <TD numeric>{run.input_tokens}</TD>
              <TD numeric>{run.output_tokens}</TD>
              <TD numeric>{formatUsd(run.estimated_cost_usd)}</TD>
              <TD>
                {run.error === null || run.error === '' ? (
                  <span className="muted">—</span>
                ) : (
                  <span className="text-error">{run.error}</span>
                )}
              </TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  )
}

/* ---------------------------------------------------------------- timeline -- */

const TIMELINE_KIND_TONES: Record<string, string> = {
  status: 'active',
  'stage.completed': 'verified',
  'stage.failed': 'failed',
  approval: 'human',
  question: 'human',
  finding: 'high',
}

export function TimelinePanel({ state }: { state: AsyncState<{ items: TimelineEntry[] }> }) {
  return (
    <AsyncBoundary state={state} context="the timeline">
      {state.data !== null ? (
        state.data.items.length === 0 ? (
          <EmptyState
            title="No timeline entries"
            description="Nothing has been recorded on the job timeline yet."
          />
        ) : (
          <ol className="timeline">
            {state.data.items.map((entry) => {
              const actor = actorTypeMeta(entry.actor_type)
              return (
                <li key={entry.id} className={`timeline__item timeline__item--${TIMELINE_KIND_TONES[entry.kind] ?? 'neutral'}`}>
                  <span className="timeline__marker" aria-hidden="true" />
                  <div className="timeline__body">
                    <div className="timeline__head">
                      <span className="timeline__summary">{entry.summary}</span>
                      <span className="muted">
                        <RelativeTime value={entry.at} />
                      </span>
                    </div>
                    <div className="timeline__meta muted">
                      <Badge tone={actor.tone} description={actor.description}>
                        {entry.actor_type}
                      </Badge>
                      <span>{entry.actor}</span>
                      <code>{entry.kind}</code>
                    </div>
                    {Object.keys(entry.metadata).length > 0 ? (
                      <details className="timeline__metadata">
                        <summary>Metadata</summary>
                        <pre>{JSON.stringify(entry.metadata, null, 2)}</pre>
                      </details>
                    ) : null}
                  </div>
                </li>
              )
            })}
          </ol>
        )
      ) : null}
    </AsyncBoundary>
  )
}

/* --------------------------------------------------------------- artifacts -- */

export function ArtifactsPanel({ state }: { state: AsyncState<{ items: Artifact[] }> }) {
  return (
    <AsyncBoundary state={state} context="artifacts">
      {state.data !== null ? (
        state.data.items.length === 0 ? (
          <EmptyState
            title="No artifacts"
            description="Raw output is stored as artifacts only when a tool or agent produced bytes worth keeping."
          />
        ) : (
          <Table caption="Artifacts for this job">
            <THead>
              <TR>
                <TH>Kind</TH>
                <TH>Filename</TH>
                <TH>Content type</TH>
                <TH numeric>Size</TH>
                <TH>SHA-256</TH>
                <TH>Created</TH>
                <TH>Expires</TH>
              </TR>
            </THead>
            <TBody>
              {state.data.items.map((item) => (
                <TR key={item.id}>
                  <TD>
                    <code>{item.kind}</code>
                  </TD>
                  <TD>{item.filename ?? <span className="muted">—</span>}</TD>
                  <TD>
                    <code>{item.content_type}</code>
                  </TD>
                  <TD numeric>{formatBytes(item.size_bytes)}</TD>
                  <TD>
                    <code title={item.sha256}>{item.sha256.slice(0, 12)}…</code>
                  </TD>
                  <TD>
                    <RelativeTime value={item.created_at} />
                  </TD>
                  <TD>
                    <RelativeTime value={item.expires_at} fallback="no expiry" />
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )
      ) : null}
    </AsyncBoundary>
  )
}

/* ----------------------------------------------------------- derived panels -- */

/**
 * Split tool runs into the sections the brief calls for.
 *
 * The API contract exposes no category field on `ToolRun` (only `tool` and the
 * free-form `summary`), so categorisation is derived from the tool name. This is
 * recorded as a contract gap in the README: a `category` on the tool run, or a
 * tool catalogue lookup by name, would be more honest than name matching.
 */
export function deriveToolSections(toolRuns: readonly ToolRun[]): {
  tests: ToolRun[]
  security: ToolRun[]
  dependencies: ToolRun[]
  other: ToolRun[]
} {
  const tests: ToolRun[] = []
  const security: ToolRun[] = []
  const dependencies: ToolRun[] = []
  const other: ToolRun[] = []

  for (const run of toolRuns) {
    const name = run.tool.toLowerCase()
    if (/pytest|jest|vitest|rspec|go test|junit|test/.test(name)) tests.push(run)
    else if (/gitleaks|bandit|semgrep|trivy|secret|sast|security|zap/.test(name)) security.push(run)
    else if (/pip-audit|npm audit|dependenc|osv|snyk|license/.test(name)) dependencies.push(run)
    else other.push(run)
  }
  return { tests, security, dependencies, other }
}

export function testsSummary(toolRuns: readonly ToolRun[]): {
  total: number
  failed: number
  infrastructureFailures: number
} {
  const { tests } = deriveToolSections(toolRuns)
  return {
    total: tests.length,
    failed: tests.filter((run) => run.status === 'failed' || run.status === 'error' || run.status === 'timeout')
      .length,
    infrastructureFailures: tests.filter((run) => run.infrastructure_failure).length,
  }
}

/** Findings filtered by category, for the security and dependency panels. */
export function findingsByCategory(
  findings: readonly Finding[],
  categories: readonly string[],
): Finding[] {
  const wanted = new Set(categories)
  return findings.filter((finding) => wanted.has(finding.category))
}

/* -------------------------------------------------- feature verification -- */

/**
 * Feature verification reads from the verification report.
 *
 * The contract given to this workstream does not define the `Report` shape (see
 * README "API contract gaps"), so this panel consumes `report.criteria`
 * defensively: unknown shapes render as "not available", and every declared
 * field the report does supply — criterion, procedure, expected_result,
 * actual_result, status, evidence — is shown when present.
 */
export function FeatureVerificationPanel({ report }: { report: unknown }) {
  const criteria = extractCriteria(report)

  if (criteria === null) {
    return (
      <EmptyState
        title="No feature verification recorded"
        description="This job has no acceptance criteria to verify, or the report did not include them. The report endpoint's shape is not part of the dashboard's API contract, so an absent value is not proof that verification was skipped."
      />
    )
  }

  if (criteria.length === 0) {
    return (
      <EmptyState
        title="The report declares no criteria"
        description="Verification ran but the report lists no acceptance criteria for this change."
      />
    )
  }

  const counts = criteria.reduce<Record<string, number>>((accumulator, criterion) => {
    const status = String(criterion.status ?? 'unknown')
    accumulator[status] = (accumulator[status] ?? 0) + 1
    return accumulator
  }, {})

  return (
    <div className="stack">
      <div className="stat-grid stat-grid--compact">
        {Object.entries(counts).map(([status, count]) => (
          <StatCard
            key={status}
            label={status.replace(/_/g, ' ')}
            value={count}
            tone={status === 'passed' ? 'good' : status === 'failed' ? 'bad' : 'warn'}
          />
        ))}
      </div>
      <p className="muted">
        A criterion may be PASSED only with both a recorded observed result and evidence. "The tests
        passed" is not the same claim as "the requested feature works".
      </p>
      {criteria.map((criterion, index) => (
        <article key={`${criterion.criterion ?? 'criterion'}-${index}`} className="criterion">
          <header className="criterion__head">
            <Badge
              tone={criterion.status === 'passed' ? 'verified' : criterion.status === 'failed' ? 'failed' : 'human'}
              description={`Criterion status ${String(criterion.status ?? 'unknown')}.`}
            >
              {String(criterion.status ?? 'UNKNOWN').replace(/_/g, ' ').toUpperCase()}
            </Badge>
            <h4 className="criterion__title">{criterion.criterion ?? `Criterion ${index + 1}`}</h4>
          </header>
          <dl className="key-values">
            {criterion.procedure !== undefined ? (
              <div className="key-values__row">
                <dt>Procedure</dt>
                <dd>{criterion.procedure}</dd>
              </div>
            ) : null}
            {criterion.expected_result !== undefined ? (
              <div className="key-values__row">
                <dt>Expected result</dt>
                <dd>{criterion.expected_result}</dd>
              </div>
            ) : null}
            <div className="key-values__row">
              <dt>Actual result</dt>
              <dd>
                {criterion.actual_result === null || criterion.actual_result === undefined || criterion.actual_result === '' ? (
                  <span className="text-error">No observed result was recorded.</span>
                ) : (
                  criterion.actual_result
                )}
              </dd>
            </div>
          </dl>
          <EvidenceList
            evidence={criterion.evidence ?? []}
            headingLevel={5}
            emptyMessage="This criterion cites no evidence, so it cannot be treated as verified."
          />
        </article>
      ))}
    </div>
  )
}

interface SafeCriterion {
  criterion?: string
  procedure?: string
  expected_result?: string
  actual_result?: string | null
  status?: string
  evidence?: import('../api/types').EvidenceReference[]
}

function extractCriteria(report: unknown): SafeCriterion[] | null {
  if (typeof report !== 'object' || report === null) return null
  const raw: unknown = (report as Record<string, unknown>)['criteria']
  if (!Array.isArray(raw)) return null
  return raw
    .filter((entry): entry is Record<string, unknown> => typeof entry === 'object' && entry !== null)
    .map((entry) => {
      const evidence = Array.isArray(entry['evidence'])
        ? (entry['evidence'] as import('../api/types').EvidenceReference[])
        : undefined
      const result: SafeCriterion = {}
      if (typeof entry['criterion'] === 'string') result.criterion = entry['criterion']
      if (typeof entry['procedure'] === 'string') result.procedure = entry['procedure']
      if (typeof entry['expected_result'] === 'string') result.expected_result = entry['expected_result']
      if (typeof entry['actual_result'] === 'string' || entry['actual_result'] === null) {
        result.actual_result = entry['actual_result'] as string | null
      }
      if (typeof entry['status'] === 'string') result.status = entry['status']
      if (evidence !== undefined) result.evidence = evidence
      return result
    })
}

/* -------------------------------------------------------------- report -- */

/** The gated recommendation and summary, plus any findings the job row omits. */
export function ReportPanel({
  state,
  job,
}: {
  state: AsyncState<import('../api/types').Report | null>
  job: JobDetail
}) {
  const knownFindingIds = useMemo(
    () => new Set(job.findings.map((finding) => finding.id)),
    [job.findings],
  )

  return (
    <AsyncBoundary state={state} context="the verification report">
      {state.data === null ? (
        <EmptyState
          title="No report yet"
          description="The verification report is produced after the pipeline finishes. A null report means it has not been generated."
        />
      ) : (
        <div className="stack">
          <dl className="key-values">
            <div className="key-values__row">
              <dt>Recommendation</dt>
              <dd>{String(state.data.recommendation ?? 'not recorded')}</dd>
            </div>
            <div className="key-values__row">
              <dt>Verification status</dt>
              <dd>{String(state.data.verification_status ?? 'not recorded')}</dd>
            </div>
            <div className="key-values__row">
              <dt>Evidence-backed findings</dt>
              <dd>
                {state.data.evidence_backed_finding_count === undefined
                  ? 'not recorded'
                  : String(state.data.evidence_backed_finding_count)}
              </dd>
            </div>
            <div className="key-values__row">
              <dt>Generated</dt>
              <dd>
                <RelativeTime value={state.data.generated_at ?? null} fallback="not recorded" />
              </dd>
            </div>
          </dl>
          {typeof state.data.summary === 'string' && state.data.summary !== '' ? (
            <p className="report__summary">{state.data.summary}</p>
          ) : null}
          {Array.isArray(state.data.unverified_areas) && state.data.unverified_areas.length > 0 ? (
            <div className="report__unverified">
              <h4>Unverified areas</h4>
              <p className="muted">
                Silence is not success. These areas were not verified, and the report states why.
              </p>
              <pre>{JSON.stringify(state.data.unverified_areas, null, 2)}</pre>
            </div>
          ) : null}
          {Array.isArray(state.data.findings) ? (
            <div>
              <h4>Findings included in the report</h4>
              {state.data.findings.filter((finding) => !knownFindingIds.has(finding.id)).length === 0 ? (
                <p className="muted">
                  Every report finding is already listed in the Findings section.
                </p>
              ) : (
                state.data.findings
                  .filter((finding) => !knownFindingIds.has(finding.id))
                  .map((finding) => <FindingCard key={finding.id} finding={finding} />)
              )}
            </div>
          ) : null}
        </div>
      )}
    </AsyncBoundary>
  )
}

/* --------------------------------------------------------------- questions -- */

export function JobQuestionsPanel({ questions }: { questions: readonly Question[] }) {
  if (questions.length === 0) {
    return (
      <EmptyState
        title="No questions"
        description="Scout has not needed to escalate anything on this job."
      />
    )
  }
  return (
    <div className="stack">
      {questions.map((question) => (
        <article key={question.id} className="job-question">
          <header className="job-question__head">
            <SeverityBadge severity={question.severity} />
            <h4 className="job-question__title">{question.title}</h4>
            <Badge
              tone={
                question.status === 'OPEN'
                  ? 'human'
                  : question.status === 'ANSWERED'
                    ? 'verified'
                    : 'neutral'
              }
              description={`Question status ${question.status}.`}
            >
              {question.status}
            </Badge>
          </header>
          <p className="job-question__description">{question.description}</p>
          {question.status === 'OPEN' ? (
            <p className="muted">
              This question is open. Answer it on the <Link to="/questions">Questions</Link> page.
              Raised <RelativeTime value={question.created_at} />.
            </p>
          ) : null}
          {question.answers.length > 0 ? (
            <ul className="job-question__answers">
              {question.answers.map((answer) => (
                <li key={answer.id}>
                  <p>{answer.body}</p>
                  <p className="muted">
                    {answer.answered_by} · <RelativeTime value={answer.answered_at} />
                  </p>
                </li>
              ))}
            </ul>
          ) : null}
          <EvidenceList evidence={question.evidence} headingLevel={5} />
        </article>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ summary -- */

export function JobStageSummary({ job }: { job: JobDetail }) {
  const outcomes = job.stages.reduce<Record<string, number>>((accumulator, stage) => {
    accumulator[stage.outcome] = (accumulator[stage.outcome] ?? 0) + 1
    return accumulator
  }, {})
  return (
    <div className="stage-summary">
      {Object.entries(outcomes).map(([outcome, count]) => {
        const meta = stageOutcomeMeta(outcome as StageOutcome)
        return (
          <span key={outcome} className="stage-summary__item">
            <Badge tone={meta.tone} description={meta.description}>
              {count} {humanizeEnum(outcome)}
            </Badge>
          </span>
        )
      })}
    </div>
  )
}
