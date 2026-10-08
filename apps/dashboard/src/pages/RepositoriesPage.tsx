import { useMemo, useState } from 'react'
import { api } from '../api/client'
import { useAsync } from '../api/hooks'
import type { ItemsResponse, RepositorySummary } from '../api/types'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { EmptyState } from '../components/EmptyState'
import { PageHeader, Panel, StatCard } from '../components/Layout'
import { RelativeTime } from '../components/RelativeTime'
import { Table, TBody, TD, TH, THead, TR } from '../components/Table'
import { RepositoryLink } from '../components/JobsTable'

function repositoriesFetcher(signal: AbortSignal): Promise<ItemsResponse<RepositorySummary>> {
  return api.listRepositories({ signal })
}

/**
 * Repository roster.
 *
 * The contract exposes no filtering parameters for this endpoint, so the search
 * box below filters the already-loaded list on the client. That is noted in the
 * README as a contract gap: server-side search would be better at scale.
 */
export function RepositoriesPage() {
  const repositories = useAsync<ItemsResponse<RepositorySummary>>(repositoriesFetcher, [])

  return (
    <>
      <PageHeader
        title="Repositories"
        description="Repositories Scout watches. A repository is active when Scout will accept webhooks for it."
        actions={
          <button type="button" className="button" onClick={repositories.refetch}>
            Refresh
          </button>
        }
      />

      <AsyncBoundary state={repositories} context="repositories" errorOverridesData>
        {repositories.data !== null ? (
          <RepositoryList items={repositories.data.items} />
        ) : null}
      </AsyncBoundary>
    </>
  )
}

function RepositoryList({ items }: { items: readonly RepositorySummary[] }) {
  const [query, setQuery] = useState('')
  const active = items.filter((item) => item.is_active).length

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase()
    if (needle === '') return [...items]
    return items.filter((item) => item.full_name.toLowerCase().includes(needle))
  }, [items, query])

  if (items.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No repositories yet"
          description="A repository appears here once Scout receives a webhook from it."
        />
      </Panel>
    )
  }

  return (
    <>
      <div className="stat-grid stat-grid--compact">
        <StatCard label="Repositories" value={items.length} hint={`${active} active`} />
        <StatCard
          label="Inactive"
          value={items.length - active}
          tone={items.length - active > 0 ? 'warn' : 'default'}
          hint="webhooks ignored"
        />
        <StatCard
          label="Jobs recorded"
          value={items.reduce((sum, item) => sum + item.job_count, 0)}
          hint="across all repositories"
        />
      </div>

      <Panel
        title="All repositories"
        description="The API contract exposes no filter parameters for this endpoint, so this search filters the loaded list in the browser."
        actions={
          <label className="field field--inline">
            <span className="field__label">Search</span>
            <input
              type="search"
              className="input"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="owner/name"
            />
          </label>
        }
        flush
      >
        {filtered.length === 0 ? (
          <EmptyState
            title="No repository matches that search"
            description="Clear the search box to see every repository."
            action={
              <button type="button" className="button button--small" onClick={() => setQuery('')}>
                Clear search
              </button>
            }
          />
        ) : (
          <Table caption="Repositories Scout watches">
            <THead>
              <TR>
                <TH>Repository</TH>
                <TH>Default branch</TH>
                <TH>Status</TH>
                <TH numeric>Jobs</TH>
                <TH>Last job</TH>
                <TH>GitHub id</TH>
              </TR>
            </THead>
            <TBody>
              {filtered.map((item) => (
                <TR key={item.full_name}>
                  <TD>
                    <RepositoryLink fullName={item.full_name} />
                  </TD>
                  <TD>
                    <code>{item.default_branch}</code>
                  </TD>
                  <TD>
                    {item.is_active ? (
                      <span className="marker marker--verified">Active</span>
                    ) : (
                      <span className="marker marker--muted">Inactive</span>
                    )}
                  </TD>
                  <TD numeric>{item.job_count}</TD>
                  <TD>
                    <RelativeTime value={item.last_job_at} fallback="never" />
                  </TD>
                  <TD>{item.github_id === null ? <span className="muted">—</span> : item.github_id}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Panel>
    </>
  )
}
