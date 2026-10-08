import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import { useAsync } from '../api/hooks'
import type { Artifact, JobDetail, JobSummary, Paginated, Report, TimelineEntry } from '../api/types'
import {
  AgentRunsPanel,
  ArtifactsPanel,
  FeatureVerificationPanel,
  JobQuestionsPanel,
  ReportPanel,
  TimelinePanel,
  ToolRunsPanel,
  deriveToolSections,
  findingsByCategory,
  testsSummary,
} from '../components/JobPanels'
import { ApprovalControl, ApprovalHistory, ApprovalValidityWarning } from '../components/Approval'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { Badge } from '../components/Badge'
import { CopyButton } from '../components/CopyButton'
import { EmptyState } from '../components/EmptyState'
import { ErrorState } from '../components/ErrorState'
import { EvidenceStrengthLegend } from '../components/EvidenceStrengthLegend'
import { FindingList } from '../components/FindingList'
import { JobsTable } from '../components/JobsTable'
import { PageHeader, Panel, StatCard } from '../components/Layout'
import { RelativeTime } from '../components/RelativeTime'
import { RiskBadge } from '../components/RiskBadge'
import { SeverityCounts } from '../components/SeverityCounts'
import { StageTimeline } from '../components/StageTimeline'
import { StatusBadge } from '../components/StatusBadge'
import { Tabs } from '../components/Tabs'
import { recommendationMeta, shortSha, shouldPollJob } from '../lib/badges'

/**
 * How often the detail page re-fetches an in-flight job. Five seconds keeps the
 * stage timeline and status badge feeling live without hammering the API; the
 * poll stops entirely once the job reaches a terminal status.
 */
const IN_FLIGHT_POLL_MS = 5_000

function JobHeader({ job, live }: { job: JobDetail; live: boolean }) {
  const recommendation = recommendationMeta(job.recommendation)
  return (
    <header className="job-header">
      <div className="job-header__title-row">
        <h2 className="job-header__title">{job.pull_request_title}</h2>
        <StatusBadge status={job.status} />
        <RiskBadge risk={job.risk_level} />
        <Badge tone={recommendation.tone} description={recommendation.description}>
          {recommendation.label}
        </Badge>
      </div>
      <dl className="job-header__facts">
        <div>
          <dt>Pull request</dt>
          <dd>
            #{job.pull_request_number}{' '}
            {job.pull_request_url !== '' ? (
              <a href={job.pull_request_url} target="_blank" rel="noreferrer noopener">
                open on GitHub
              </a>
            ) : null}
          </dd>
        </div>
        <div>
          <dt>Repository</dt>
          <dd>{job.repository_full_name}</dd>
        </div>
        <div>
          <dt>Author</dt>
          <dd>{job.pull_request_author}</dd>
        </div>
        <div>
          <dt>Head commit</dt>
          <dd>
            <code title={job.head_sha}>{shortSha(job.head_sha, 12)}</code>{' '}
            <CopyButton value={job.head_sha} label="Copy" subject="head commit SHA" />
          </dd>
        </div>
        <div>
          <dt>Base commit</dt>
          <dd>
            <code title={job.commits.base_sha}>{shortSha(job.commits.base_sha, 12)}</code>
          </dd>
        </div>
        <div>
          <dt>Merge base</dt>
          <dd>
            {job.commits.merge_base_sha === null ? (
              <span className="muted">not recorded</span>
            ) : (
              <code title={job.commits.merge_base_sha}>
                {shortSha(job.commits.merge_base_sha, 12)}
              </code>
            )}
          </dd>
        </div>
        <div>
          <dt>Attempt</dt>
          <dd>
            {job.attempt} of {job.max_attempts}
          </dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>
            <RelativeTime value={job.created_at} />
          </dd>
        </div>
        <div>
          <dt>Verification</dt>
          <dd>{job.verification_status.replace(/_/g, ' ')}</dd>
        </div>
        <div>
          <dt>Job id</dt>
          <dd>
            <code>{job.id}</code> <CopyButton value={job.id} label="Copy" subject="job id" />
          </dd>
        </div>
      </dl>
      <p className="job-header__live muted" aria-live="polite">
        {live ? `Live — polling every ${IN_FLIGHT_POLL_MS / 1000} seconds.` : 'Polling stopped: this job is no longer in flight.'}
      </p>
      {job.failure_reason !== null && job.failure_reason !== '' ? (
        <p className="job-header__failure text-error">
          <strong>Failure reason:</strong> {job.failure_reason}
        </p>
      ) : null}
      {job.cancel_reason !== null && job.cancel_reason !== '' ? (
        <p className="job-header__cancel muted">
          <strong>Cancel reason:</strong> {job.cancel_reason}
        </p>
      ) : null}
    </header>
  )
}

function JobActions({ job, onUpdated }: { job: JobDetail; onUpdated: (job: JobDetail) => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState<'cancel' | 'retry' | null>(null)
  const [error, setError] = useState<ApiError | Error | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const canCancel = !['COMPLETED', 'CANCELLED'].includes(job.status)
  // FAILED is a resting state rather than a tombstone, so retry stays available.
  const canRetry = job.status === 'FAILED' || job.status === 'CANCELLED'

  const run = async (action: 'cancel' | 'retry'): Promise<void> => {
    setError(null)
    setNotice(null)
    if (reason.trim() === '') {
      setError(new Error('A reason is required: it is written to the audit log.'))
      return
    }
    setBusy(action)
    try {
      const payload = { reason: reason.trim() }
      const updated =
        action === 'cancel' ? await api.cancelJob(job.id, payload) : await api.retryJob(job.id, payload)
      onUpdated(updated)
      setNotice(action === 'cancel' ? 'Job cancelled.' : 'Job re-queued for another attempt.')
      setReason('')
    } catch (caught) {
      setError(caught instanceof Error ? caught : new Error(String(caught)))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Panel title="Job actions" description="Every privileged action is written to the audit log.">
      <label className="field" htmlFor="job-action-reason">
        <span className="field__label">Reason</span>
        <input
          id="job-action-reason"
          className="input"
          type="text"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="Why you are cancelling or retrying this job"
        />
      </label>
      <div className="button-row">
        <button
          type="button"
          className="button"
          disabled={!canCancel || busy !== null}
          onClick={() => {
            void run('cancel')
          }}
        >
          {busy === 'cancel' ? 'Cancelling…' : 'Cancel job'}
        </button>
        <button
          type="button"
          className="button"
          disabled={!canRetry || busy !== null}
          onClick={() => {
            void run('retry')
          }}
        >
          {busy === 'retry' ? 'Re-queueing…' : 'Retry job'}
        </button>
      </div>
      {!canRetry ? (
        <p className="muted field__hint">
          Retry becomes available when the job is FAILED or CANCELLED. A failed job is a resting state,
          not a tombstone: an operator may explicitly re-queue it.
        </p>
      ) : null}
      <div aria-live="polite">
        {error !== null ? (
          <p className="form-error" role="alert">
            {error instanceof ApiError ? error.displayMessage : error.message}
          </p>
        ) : null}
        {notice !== null ? <p className="form-success">{notice}</p> : null}
      </div>
    </Panel>
  )
}

/** Sibling jobs for the same repository, so an operator can see the PR's history. */
function RelatedJobs({ job }: { job: JobDetail }) {
  const siblings = useAsync<Paginated<JobSummary>>(
    (signal) =>
      api.listJobs({ repository: job.repository_full_name, limit: 5, offset: 0 }, { signal }),
    [job.repository_full_name],
  )

  return (
    <Panel title="Recent jobs in this repository" flush>
      <AsyncBoundary state={siblings} context="sibling jobs">
        {siblings.data !== null ? (
          siblings.data.items.length === 0 ? (
            <EmptyState title="No other jobs" description="This is the only job for this repository." />
          ) : (
            <JobsTable jobs={siblings.data.items} showRepository={false} />
          )
        ) : null}
      </AsyncBoundary>
    </Panel>
  )
}

/**
 * Job detail — the dashboard's most important page.
 *
 * Order of the page is deliberate and follows what an operator must decide:
 *
 * 1. The invalidated-approval warning, if there is one, before anything else.
 * 2. The header: what change, which commit, and what Scout recommends.
 * 3. The human authorization control, which is the only authority on the page.
 * 4. The stage timeline, so the state of the pipeline is visible.
 * 5. Sectioned panels for findings, tests, security, dependencies, feature
 *    verification, questions, timeline, artifacts, agent runs, and tool runs.
 *
 * Polling is 5 seconds while the job is in flight and stops at a terminal status
 * (COMPLETED or CANCELLED). A FAILED job keeps polling because an operator may
 * re-queue it, so it can still move.
 */
export function JobDetailPage() {
  const params = useParams<{ jobId: string }>()
  const jobId = params.jobId ?? ''

  // One polling hook. `pollMs` is derived from the last known status and is part
  // of the dependency list, so the effect restarts when the interval changes and
  // the interval is cleared entirely once the job reaches a terminal status.
  // `shouldPollJob` excludes COMPLETED and CANCELLED but keeps FAILED, because
  // an operator may re-queue a failed job (spec section 51).
  const [pollMs, setPollMs] = useState(IN_FLIGHT_POLL_MS)

  const job = useAsync<JobDetail>((signal) => api.getJob(jobId, { signal }), [jobId, pollMs], {
    pollMs,
    enabled: jobId !== '',
  })

  useEffect(() => {
    const status = job.data?.status
    if (status === undefined) return
    setPollMs(shouldPollJob(status) ? IN_FLIGHT_POLL_MS : 0)
  }, [job.data?.status])

  const current = job.data
  const live = current !== null && shouldPollJob(current.status)

  const report = useAsync<Report | null>((signal) => api.getReport(jobId, { signal }), [jobId], {
    enabled: jobId !== '',
  })
  const timeline = useAsync<{ items: TimelineEntry[] }>(
    (signal) => api.getTimeline(jobId, { signal }),
    [jobId],
    { enabled: jobId !== '' },
  )
  const artifacts = useAsync<{ items: Artifact[] }>(
    (signal) => api.listArtifacts(jobId, { signal }),
    [jobId],
    { enabled: jobId !== '' },
  )

  if (jobId === '') {
    return (
      <ErrorState
        error={new ApiError({
          message: 'The URL is missing a job id.',
          status: 0,
          kind: 'http',
          body: null,
          path: '/jobs',
        })}
      />
    )
  }

  if (job.status === 'loading' && job.data === null) {
    return (
      <>
        <PageHeader title="Job detail" />
        <Panel>
          <div className="loading loading--block" role="status" aria-live="polite">
            <span className="spinner" aria-hidden="true" />
            <span>Loading job {jobId}…</span>
          </div>
        </Panel>
      </>
    )
  }

  if (current === null) {
    return (
      <>
        <PageHeader
          title="Job detail"
          actions={<Link to="/jobs">Back to jobs</Link>}
        />
        <ErrorState error={job.error} context={`job ${jobId}`} onRetry={job.refetch} />
      </>
    )
  }

  const resolvedJob = current
  const sections = deriveToolSections(resolvedJob.tool_runs)
  const securityFindings = findingsByCategory(resolvedJob.findings, ['SECURITY', 'SECRETS'])
  const dependencyFindings = findingsByCategory(resolvedJob.findings, ['DEPENDENCIES'])
  const openQuestions = resolvedJob.questions.filter((question) => question.status === 'OPEN')

  const updateFromAction = (updated: JobDetail): void => {
    job.setData(updated)
    report.refetch()
    timeline.refetch()
  }

  const [repoOwner, repoName] = resolvedJob.repository_full_name.split('/')

  return (
    <>
      <PageHeader
        title={`PR #${resolvedJob.pull_request_number}`}
        description={
          <>
            <Link to="/jobs">Jobs</Link> /{' '}
            {repoOwner !== undefined && repoName !== undefined ? (
              <Link
                to={`/repositories/${encodeURIComponent(repoOwner)}/${encodeURIComponent(repoName)}`}
              >
                {resolvedJob.repository_full_name}
              </Link>
            ) : (
              resolvedJob.repository_full_name
            )}{' '}
            / {shortSha(resolvedJob.head_sha, 12)}
          </>
        }
        actions={
          <button type="button" className="button" onClick={job.refetch}>
            Refresh
          </button>
        }
      />

      {job.error !== null ? (
        <div className="banner banner--warning" role="status">
          <strong>Refresh failed ({job.error.status || 'no response'}).</strong>{' '}
          {job.error.displayMessage} The values below are from the last successful load.
        </div>
      ) : null}

      <ApprovalValidityWarning job={resolvedJob} />

      <JobHeader job={resolvedJob} live={live} />

      <div className="grid grid--sidebar">
        <div className="stack">
          <div className="stat-grid stat-grid--compact">
            <StatCard
              label="Findings"
              value={resolvedJob.findings.length}
              hint={<SeverityCounts counts={resolvedJob.finding_counts} />}
            />
            <StatCard label="Open questions" value={resolvedJob.open_question_count} tone={resolvedJob.open_question_count > 0 ? 'warn' : 'default'} />
            <StatCard label="Tool runs" value={resolvedJob.tool_runs.length} />
            <StatCard label="Agent runs" value={resolvedJob.agent_runs.length} />
          </div>

          <Panel
            title="Stage pipeline"
            description="Status is derived from the first unresolved stage. A skip is a recorded outcome with a reason, never silence."
          >
            <StageTimeline stages={resolvedJob.stages} currentStatus={resolvedJob.status} />
          </Panel>

          <ApprovalControl job={resolvedJob} onDecided={updateFromAction} />

          <Panel title="Sections" flush>
            <Tabs
              label="Job detail sections"
              tabs={[
                {
                  id: 'findings',
                  label: 'Findings',
                  count: resolvedJob.findings.length,
                  content: () => (
                    <div className="stack">
                      <EvidenceStrengthLegend />
                      <FindingList
                        findings={resolvedJob.findings}
                        emptyTitle="No findings"
                        emptyDescription="Scout recorded no findings for this job. That is not the same as proving the change correct — check the unverified areas in the report."
                      />
                    </div>
                  ),
                },
                {
                  id: 'tests',
                  label: 'Tests',
                  count: sections.tests.length,
                  content: () => {
                    const summary = testsSummary(resolvedJob.tool_runs)
                    return (
                      <div className="stack">
                        <div className="stat-grid stat-grid--compact">
                          <StatCard label="Test tool runs" value={summary.total} />
                          <StatCard
                            label="Failed"
                            value={summary.failed}
                            tone={summary.failed > 0 ? 'bad' : 'good'}
                            hint="failing test runs"
                          />
                          <StatCard
                            label="Infrastructure failures"
                            value={summary.infrastructureFailures}
                            tone={summary.infrastructureFailures > 0 ? 'warn' : 'default'}
                            hint="not code findings"
                          />
                        </div>
                        <p className="muted">
                          A deterministic test run is MACHINE-strength evidence. A test tool that failed
                          for infrastructure reasons is reported as such and is not a code finding.
                        </p>
                        <ToolRunsPanel toolRuns={sections.tests} />
                      </div>
                    )
                  },
                },
                {
                  id: 'security',
                  label: 'Security',
                  count: securityFindings.length + sections.security.length,
                  content: () => (
                    <div className="stack">
                      <p className="muted">
                        Secret scanning and static analysis. These categories are derived from the tool
                        name because the contract exposes no category on a tool run.
                      </p>
                      <ToolRunsPanel toolRuns={sections.security} />
                      <FindingList
                        findings={securityFindings}
                        showFilters={false}
                        emptyTitle="No security findings"
                        emptyDescription="No security or secrets finding was recorded for this job."
                      />
                    </div>
                  ),
                },
                {
                  id: 'dependencies',
                  label: 'Dependencies',
                  count: dependencyFindings.length + sections.dependencies.length,
                  content: () => (
                    <div className="stack">
                      <ToolRunsPanel toolRuns={sections.dependencies} />
                      <FindingList
                        findings={dependencyFindings}
                        showFilters={false}
                        emptyTitle="No dependency findings"
                        emptyDescription="No dependency finding was recorded for this job."
                      />
                    </div>
                  ),
                },
                {
                  id: 'feature-verification',
                  label: 'Feature verification',
                  content: () => <FeatureVerificationPanel report={report.data} />,
                },
                {
                  id: 'report',
                  label: 'Report',
                  content: () => <ReportPanel state={report} job={resolvedJob} />,
                },
                {
                  id: 'questions',
                  label: 'Questions',
                  count: resolvedJob.questions.length,
                  content: () => <JobQuestionsPanel questions={resolvedJob.questions} />,
                },
                {
                  id: 'timeline',
                  label: 'Timeline',
                  count: timeline.data?.items.length,
                  content: () => <TimelinePanel state={timeline} />,
                },
                {
                  id: 'artifacts',
                  label: 'Artifacts',
                  count: artifacts.data?.items.length,
                  content: () => <ArtifactsPanel state={artifacts} />,
                },
                {
                  id: 'agents',
                  label: 'Agent runs',
                  count: resolvedJob.agent_runs.length,
                  content: () => <AgentRunsPanel agentRuns={resolvedJob.agent_runs} />,
                },
                {
                  id: 'tools',
                  label: 'Tool runs',
                  count: resolvedJob.tool_runs.length,
                  content: () => <ToolRunsPanel toolRuns={resolvedJob.tool_runs} />,
                },
              ]}
            />
          </Panel>
        </div>

        <aside className="job-sidebar stack">
          <Panel
            title="Scout recommendation"
            description="Gated by evidence. Cannot be loosened after the report is generated."
          >
            <p className="job-sidebar__recommendation">
              <Badge
                tone={recommendationMeta(resolvedJob.recommendation).tone}
                description={recommendationMeta(resolvedJob.recommendation).description}
              >
                {recommendationMeta(resolvedJob.recommendation).label}
              </Badge>
            </p>
            <p className="muted">
              Scout recommends; a human authorizes. Scout never merges, deploys, or approves its own
              work.
            </p>
          </Panel>

          <Panel title="Approval record" description={`${resolvedJob.approvals.length} recorded decision(s).`}>
            {resolvedJob.current_approval_valid ? (
              <p className="marker marker--verified">A valid approval covers the current commit.</p>
            ) : resolvedJob.approvals.length > 0 ? (
              <p className="marker marker--invalidated">No valid approval covers the current commit.</p>
            ) : (
              <p className="muted">No human decision has been recorded.</p>
            )}
            <ApprovalHistory job={resolvedJob} />
          </Panel>

          {openQuestions.length > 0 ? (
            <Panel title="Open questions">
              <ul className="compact-list">
                {openQuestions.map((question) => (
                  <li key={question.id}>{question.title}</li>
                ))}
              </ul>
              <p>
                <Link to="/questions">Answer them on the Questions page</Link>
              </p>
            </Panel>
          ) : null}

          <JobActions job={resolvedJob} onUpdated={updateFromAction} />
        </aside>
      </div>

      <RelatedJobs job={resolvedJob} />
    </>
  )
}
