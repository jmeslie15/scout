import { Link, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useAsync } from '../api/hooks'
import type { JobSummary, RepositoryDetail } from '../api/types'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { EmptyState } from '../components/EmptyState'
import { JobsTable } from '../components/JobsTable'
import { PageHeader, Panel, StatCard } from '../components/Layout'
import { RelativeTime } from '../components/RelativeTime'

/**
 * Repository detail.
 *
 * The contract specifies "repository detail with its recent jobs" but does not
 * pin the field name for those jobs. `recent_jobs` is used as the primary name
 * and the component renders an explicit gap notice rather than nothing if the
 * server returns a different shape — inventing a fallback field name would hide
 * the mismatch.
 */
export function RepositoryDetailPage() {
  const params = useParams<{ owner: string; name: string }>()
  const owner = params.owner ?? ''
  const name = params.name ?? ''
  const fullName = `${owner}/${name}`

  const repository = useAsync<RepositoryDetail>(
    (signal) => api.getRepository(owner, name, { signal }),
    [owner, name],
    { enabled: owner !== '' && name !== '' },
  )

  const recentJobs: JobSummary[] = repository.data?.recent_jobs ?? []

  return (
    <>
      <PageHeader
        title={fullName}
        description={
          <>
            <Link to="/repositories">Repositories</Link> / {fullName}
          </>
        }
        actions={
          <button type="button" className="button" onClick={repository.refetch}>
            Refresh
          </button>
        }
      />

      <AsyncBoundary state={repository} context={`repository ${fullName}`} errorOverridesData>
        {repository.data !== null ? (
          <>
            <div className="stat-grid stat-grid--compact">
              <StatCard label="Jobs recorded" value={repository.data.job_count} />
              <StatCard
                label="Status"
                value={repository.data.is_active ? 'Active' : 'Inactive'}
                tone={repository.data.is_active ? 'good' : 'warn'}
                hint={repository.data.is_active ? 'webhooks accepted' : 'webhooks ignored'}
              />
              <StatCard
                label="Default branch"
                value={<code>{repository.data.default_branch}</code>}
              />
              <StatCard
                label="Last job"
                value={<RelativeTime value={repository.data.last_job_at} fallback="never" />}
              />
              <StatCard
                label="GitHub id"
                value={repository.data.github_id === null ? '—' : repository.data.github_id}
              />
            </div>

            <Panel title="Recent jobs" description="Newest first." flush>
              {recentJobs.length === 0 ? (
                <EmptyState
                  title="No jobs returned for this repository"
                  description="Either this repository has no verification jobs yet, or the detail endpoint returned its jobs under a field name this build does not recognise."
                />
              ) : (
                <JobsTable jobs={recentJobs} showRepository={false} />
              )}
            </Panel>

            <Panel title="All jobs in this repository">
              <p>
                <Link to={`/jobs?repository=${encodeURIComponent(fullName)}`}>
                  Open the full job list filtered to this repository
                </Link>
              </p>
            </Panel>
          </>
        ) : null}
      </AsyncBoundary>
    </>
  )
}
