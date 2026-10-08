import { Link } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../api/hooks'
import { ACTIVE_JOB_STATUSES, ALL_SEVERITIES } from '../api/types'
import type { MetricsSummary, Paginated, JobSummary } from '../api/types'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { Badge } from '../components/Badge'
import { JobsTable } from '../components/JobsTable'
import { PageHeader, Panel, StatCard } from '../components/Layout'
import { useReadiness } from '../components/readinessContext'
import {
  formatCompactNumber,
  formatDuration,
  formatUsd,
  humanizeEnum,
  jobStatusMeta,
  severityMeta,
} from '../lib/badges'

const RECENT_JOB_LIMIT = 10

/** Horizontal proportion bar used in the two breakdown panels. */
function BreakdownRow({
  label,
  count,
  total,
  tone,
}: {
  label: string
  count: number
  total: number
  tone: string
}) {
  const pct = total > 0 ? Math.round((count / total) * 100) : 0
  return (
    <li className="breakdown__row">
      <span className="breakdown__label">{label}</span>
      <span className={`breakdown__bar breakdown__bar--${tone}`}>
        <span className="breakdown__fill" style={{ width: `${pct}%` }} />
      </span>
      <span className="breakdown__value">
        {count}
        <span className="muted"> ({pct}%)</span>
      </span>
    </li>
  )
}

/**
 * Dashboard: fleet-wide metrics, jobs-by-status and findings-by-severity
 * breakdowns, and the ten most recent jobs.
 *
 * Readiness comes from the shell's context — the shell owns the poll and the
 * outage banner, so the dashboard reads it rather than issuing a second request.
 */
export function DashboardPage() {
  const readiness = useReadiness()

  const metrics = useAsync<MetricsSummary>((signal) => api.metricsSummary({ signal }), [], {
    pollMs: 30_000,
  })

  const recent = useAsync<Paginated<JobSummary>>(
    (signal) => api.listJobs({ limit: RECENT_JOB_LIMIT, offset: 0 }, { signal }),
    [],
  )

  const summary = metrics.data
  const totalJobs = summary?.jobs_total ?? 0
  const jobsByStatus = summary?.jobs_by_status ?? {}
  const findingsBySeverity = summary?.findings_by_severity ?? {}
  const jobsInFlight = ACTIVE_JOB_STATUSES.reduce(
    (sum, status) => sum + (jobsByStatus[status] ?? 0),
    0,
  )
  const failureRate =
    totalJobs > 0 && summary !== null ? Math.round((summary.jobs_failed / totalJobs) * 100) : null

  const statusTotal = Object.values(jobsByStatus).reduce((sum, value) => sum + value, 0)
  const severityTotal = Object.values(findingsBySeverity).reduce((sum, value) => sum + value, 0)

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Scout verifies pull requests, records the evidence behind every conclusion, and recommends. A human authorizes."
        actions={
          <button
            type="button"
            className="button"
            onClick={() => {
              metrics.refetch()
              recent.refetch()
            }}
          >
            Refresh
          </button>
        }
      />

      {readiness !== null && readiness.readiness !== null && readiness.readiness.status !== 'ready' ? (
        <div className="banner banner--warning" role="status">
          <strong>Verification is degraded.</strong> One or more API dependencies are not ready, so
          jobs may stall or fail. See the readiness details in the header.
        </div>
      ) : null}

      <AsyncBoundary state={metrics} context="metrics" errorOverridesData>
        {summary !== null ? (
          <>
            <div className="stat-grid">
              <StatCard
                label="Jobs total"
                value={summary.jobs_total}
                hint={`${jobsInFlight} in flight`}
              />
              <StatCard
                label="Jobs failed"
                value={summary.jobs_failed}
                tone={summary.jobs_failed > 0 ? 'bad' : 'good'}
                hint={failureRate === null ? 'no jobs yet' : `${failureRate}% of all jobs`}
              />
              <StatCard
                label="Average job duration"
                value={formatDuration(summary.avg_job_duration_seconds)}
                hint="start to completion"
              />
              <StatCard
                label="Test failures"
                value={summary.test_failures}
                tone={summary.test_failures > 0 ? 'warn' : 'good'}
                hint="deterministic test runs"
              />
              <StatCard
                label="Human escalations"
                value={summary.human_escalations}
                tone={summary.human_escalations > 0 ? 'warn' : 'default'}
                hint="questions awaiting an answer"
              />
              <StatCard
                label="LLM tokens"
                value={formatCompactNumber(summary.llm_tokens)}
                hint={`${formatUsd(summary.llm_cost_usd)} estimated cost`}
              />
            </div>

            <div className="grid grid--two">
              <Panel
                title="Jobs by status"
                description="Where every job currently sits in the pipeline."
                flush
              >
                {statusTotal === 0 ? (
                  <p className="muted panel__empty">No jobs recorded yet.</p>
                ) : (
                  <ul className="breakdown">
                    {Object.entries(jobsByStatus)
                      .sort((a, b) => b[1] - a[1])
                      .map(([status, count]) => (
                        <BreakdownRow
                          key={status}
                          label={jobStatusMeta(status).label}
                          tone={jobStatusMeta(status).tone}
                          count={count}
                          total={statusTotal}
                        />
                      ))}
                  </ul>
                )}
              </Panel>

              <Panel
                title="Findings by severity"
                description="CRITICAL and HIGH findings gate the verification recommendation."
                flush
              >
                {severityTotal === 0 ? (
                  <p className="muted panel__empty">No findings recorded yet.</p>
                ) : (
                  <ul className="breakdown">
                    {ALL_SEVERITIES.map((severity) => {
                      const count = findingsBySeverity[severity] ?? 0
                      const meta = severityMeta(severity)
                      return (
                        <BreakdownRow
                          key={severity}
                          label={meta.label}
                          tone={meta.tone}
                          count={count}
                          total={severityTotal}
                        />
                      )
                    })}
                  </ul>
                )}
              </Panel>
            </div>
          </>
        ) : null}
      </AsyncBoundary>

      <Panel
        title="Ten most recent jobs"
        description="Newest first."
        actions={<Link to="/jobs">All jobs</Link>}
        flush
      >
        <AsyncBoundary state={recent} context="recent jobs">
          {recent.data !== null ? (
            <JobsTable
              jobs={recent.data.items}
              emptyTitle="No verification jobs yet"
              emptyDescription="Jobs appear here once GitHub sends a pull-request webhook."
            />
          ) : null}
        </AsyncBoundary>
      </Panel>

      <div className="grid grid--two">
        <Panel title="Pipeline reference" description="The pipeline every job moves through.">
          <ol className="pipeline-reference">
            {[
              'PREPARING',
              'ANALYZING',
              'REVIEWING',
              'SECURITY_SCANNING',
              'TESTING',
              'FEATURE_VERIFYING',
              'INVESTIGATING',
            ].map((stage) => (
              <li key={stage}>
                <span className="pipeline-reference__name">{humanizeEnum(stage)}</span>
              </li>
            ))}
          </ol>
          <p className="muted">
            Status is derived from the first unresolved stage. A stage is resolved when its outcome is
            SUCCEEDED or SKIPPED, and a skip always carries a recorded reason.
          </p>
        </Panel>

        <Panel title="Evidence discipline" description="Why an AI-only finding is never a verified fact.">
          <ul className="prose-list">
            <li>
              <Badge tone="deterministic">MACHINE</Badge> Deterministic execution — a tool run, test
              run, git diff, GitHub check, or stored artifact. Cannot be argued with.
            </li>
            <li>
              <Badge tone="verified">INSPECTION</Badge> Direct observation of a file or repository
              structure, pinned to a path and line range.
            </li>
            <li>
              <Badge tone="human">AUTHORITATIVE</Badge> A human answer or an approved decision.
              Outranks anything Scout inferred.
            </li>
            <li>
              <Badge tone="ai">INFERENCE</Badge> An AI claim. Confidence is capped at 0.65 and a
              finding resting on it alone is never marked a verified fact.
            </li>
          </ul>
        </Panel>
      </div>
    </>
  )
}
