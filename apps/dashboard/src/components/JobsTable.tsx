import { Link } from 'react-router-dom'
import type { JobSummary } from '../api/types'
import { formatDuration, recommendationMeta, shortSha } from '../lib/badges'
import { Badge } from './Badge'
import { EmptyState } from './EmptyState'
import { RelativeTime } from './RelativeTime'
import { RiskBadge } from './RiskBadge'
import { SeverityCounts } from './SeverityCounts'
import { StatusBadge } from './StatusBadge'
import { Table, TBody, TD, TH, THead, TR } from './Table'

export interface JobsTableProps {
  jobs: readonly JobSummary[]
  emptyTitle?: string
  emptyDescription?: string
  /** Show the Repository column. Hidden on repository detail, where it is fixed. */
  showRepository?: boolean
}

/**
 * The canonical jobs table, used by the dashboard, the jobs list, and repository
 * detail. Columns follow the brief: PR, Repository, Commit, Risk, Status,
 * Verification, Recommendation, Findings, Questions, Created, Duration.
 *
 * The PR title links to the GitHub pull request (`pull_request_url`) because
 * that is the artifact a reviewer actually needs; the whole title links to the
 * Scout job detail page.
 */
export function JobsTable({
  jobs,
  emptyTitle = 'No jobs',
  emptyDescription = 'No verification job matches this view yet.',
  showRepository = true,
}: JobsTableProps) {
  if (jobs.length === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} />
  }

  return (
    <Table caption="Verification jobs">
      <THead>
        <TR>
          <TH>PR</TH>
          {showRepository ? <TH>Repository</TH> : null}
          <TH>Commit</TH>
          <TH>Risk</TH>
          <TH>Status</TH>
          <TH>Verification</TH>
          <TH>Recommendation</TH>
          <TH>Findings</TH>
          <TH numeric>Questions</TH>
          <TH>Created</TH>
          <TH numeric>Duration</TH>
        </TR>
      </THead>
      <TBody>
        {jobs.map((job) => {
          const recommendation = recommendationMeta(job.recommendation)
          const duration =
            job.started_at !== null && job.completed_at !== null
              ? (new Date(job.completed_at).getTime() - new Date(job.started_at).getTime()) / 1000
              : null
          return (
            <TR key={job.id}>
              <TD>
                <div className="job-cell">
                  <Link className="job-cell__title" to={`/jobs/${job.id}`}>
                    {job.pull_request_title}
                  </Link>
                  <span className="job-cell__meta muted">
                    #{job.pull_request_number}
                    {job.pull_request_url !== '' ? (
                      <>
                        {' · '}
                        <a href={job.pull_request_url} target="_blank" rel="noreferrer noopener">
                          GitHub
                        </a>
                      </>
                    ) : null}
                  </span>
                </div>
              </TD>
              {showRepository ? (
                <TD>
                  <RepositoryLink fullName={job.repository_full_name} />
                </TD>
              ) : null}
              <TD>
                <code title={job.head_sha}>{shortSha(job.head_sha)}</code>
              </TD>
              <TD>
                <RiskBadge risk={job.risk_level} />
              </TD>
              <TD>
                <StatusBadge status={job.status} />
              </TD>
              <TD>
                <span className="muted">{job.verification_status.replace(/_/g, ' ')}</span>
              </TD>
              <TD>
                {job.recommendation === null || job.recommendation === '' ? (
                  <span className="muted">—</span>
                ) : (
                  <Badge tone={recommendation.tone} description={recommendation.description}>
                    {recommendation.label}
                  </Badge>
                )}
              </TD>
              <TD>
                <SeverityCounts counts={job.finding_counts} />
              </TD>
              <TD numeric>
                {job.open_question_count > 0 ? (
                  <span className="emphasis">{job.open_question_count}</span>
                ) : (
                  <span className="muted">0</span>
                )}
              </TD>
              <TD>
                <RelativeTime value={job.created_at} />
              </TD>
              <TD numeric>{formatDuration(duration)}</TD>
            </TR>
          )
        })}
      </TBody>
    </Table>
  )
}

/** Link to a repository detail page, splitting `owner/name` safely. */
export function RepositoryLink({ fullName }: { fullName: string }) {
  const [owner, name] = fullName.split('/')
  if (owner === undefined || name === undefined || owner === '' || name === '') {
    return <span>{fullName}</span>
  }
  return (
    <Link to={`/repositories/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`}>
      {fullName}
    </Link>
  )
}
