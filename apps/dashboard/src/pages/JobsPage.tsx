import { api } from '../api/client'
import { useAsync } from '../api/hooks'
import { ALL_JOB_STATUSES } from '../api/types'
import type { ItemsResponse, JobSummary, Paginated, RepositorySummary } from '../api/types'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { EmptyState } from '../components/EmptyState'
import { JobsTable } from '../components/JobsTable'
import { PageHeader, Panel } from '../components/Layout'
import { Pagination } from '../components/Pagination'
import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS, normalizePage } from '../lib/pagination'
import { useQueryState } from '../lib/useQueryState'

/**
 * Jobs list.
 *
 * Filters and pagination are URL state (`useQueryState`) so a filtered view is a
 * shareable link. Filters are sent to the server (`status`, `repository`) rather
 * than applied in the browser, because the endpoint supports them and the job
 * table is unbounded, so client-side filtering would silently hide matches on
 * other pages.
 */
export function JobsPage() {
  const query = useQueryState()

  const status = query.get('status', '') ?? ''
  const repository = query.get('repository', '') ?? ''
  const page = normalizePage(query.getNumber('page', 1))
  const rawPageSize = query.getNumber('pageSize', DEFAULT_PAGE_SIZE)
  const pageSize = PAGE_SIZE_OPTIONS.includes(rawPageSize) ? rawPageSize : DEFAULT_PAGE_SIZE
  const offset = (page - 1) * pageSize

  const jobs = useAsync<Paginated<JobSummary>>(
    (signal) => api.listJobs({ status, repository, limit: pageSize, offset }, { signal }),
    [status, repository, pageSize, offset],
  )

  const repositories = useAsync<ItemsResponse<RepositorySummary>>(
    (signal) => api.listRepositories({ signal }),
    [],
  )

  const filtersActive = status !== '' || repository !== ''
  const repositoryOptions = repositories.data?.items ?? []

  return (
    <>
      <PageHeader
        title="Jobs"
        description="Every verification job, newest first. Filter by status or repository; the filter is kept in the URL so the view is shareable."
        actions={
          <button type="button" className="button" onClick={jobs.refetch}>
            Refresh
          </button>
        }
      />

      <Panel title="Filters">
        <div className="filter-bar">
          <label className="field">
            <span className="field__label">Status</span>
            <select
              className="select"
              value={status}
              onChange={(event) => {
                query.update({ status: event.target.value, page: 1 })
              }}
            >
              <option value="">All statuses</option>
              {ALL_JOB_STATUSES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>

          <label className="field">
            <span className="field__label">Repository</span>
            <select
              className="select"
              value={repository}
              onChange={(event) => {
                query.update({ repository: event.target.value, page: 1 })
              }}
            >
              <option value="">All repositories</option>
              {repositoryOptions.map((item) => (
                <option key={item.full_name} value={item.full_name}>
                  {item.full_name}
                </option>
              ))}
            </select>
            {repositories.error !== null ? (
              <span className="field__hint field__hint--warn">
                The repository list could not be loaded (HTTP {repositories.error.status || 'no response'}
                ), so only the status filter is available.{' '}
                <button type="button" className="link-button" onClick={repositories.refetch}>
                  Retry
                </button>
              </span>
            ) : null}
          </label>

          <label className="field">
            <span className="field__label">Rows per page</span>
            <select
              className="select"
              value={pageSize}
              onChange={(event) => {
                query.update({ pageSize: Number(event.target.value), page: 1 })
              }}
            >
              {PAGE_SIZE_OPTIONS.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>

          {filtersActive ? (
            <button
              type="button"
              className="button button--ghost"
              onClick={() => query.clear(['status', 'repository', 'page'])}
            >
              Clear filters
            </button>
          ) : null}
        </div>
      </Panel>

      <Panel flush>
        <AsyncBoundary state={jobs} context="jobs">
          {jobs.data !== null ? (
            jobs.data.items.length === 0 ? (
              <EmptyState
                title={filtersActive ? 'No job matches these filters' : 'No verification jobs yet'}
                description={
                  filtersActive
                    ? 'Widen the status or repository filter to see more jobs.'
                    : 'Jobs appear here once GitHub sends a pull-request webhook.'
                }
                action={
                  filtersActive ? (
                    <button
                      type="button"
                      className="button button--small"
                      onClick={() => query.clear(['status', 'repository', 'page'])}
                    >
                      Clear filters
                    </button>
                  ) : undefined
                }
              />
            ) : (
              <JobsTable jobs={jobs.data.items} />
            )
          ) : null}
        </AsyncBoundary>
      </Panel>

      {jobs.data !== null ? (
        <Pagination
          page={page}
          pageSize={pageSize}
          total={jobs.data.total}
          busy={jobs.refreshing}
          onPageChange={(next) => {
            query.update({ page: next < 1 ? 1 : next })
          }}
          onPageSizeChange={(size) => query.update({ pageSize: size, page: 1 })}
        />
      ) : null}

      {jobs.data !== null && jobs.refreshing ? (
        <p className="muted" aria-live="polite">
          Refreshing…
        </p>
      ) : null}
    </>
  )
}
