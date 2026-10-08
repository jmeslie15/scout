import { api } from '../api/client'
import { useAsync } from '../api/hooks'
import type { AuditEvent, Paginated } from '../api/types'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { Badge } from '../components/Badge'
import { EmptyState } from '../components/EmptyState'
import { PageHeader, Panel } from '../components/Layout'
import { Pagination } from '../components/Pagination'
import { RelativeTime } from '../components/RelativeTime'
import { actorTypeMeta } from '../lib/badges'
import { DEFAULT_PAGE_SIZE, normalizePage } from '../lib/pagination'
import { useQueryState } from '../lib/useQueryState'

/**
 * Audit log — read-only by design.
 *
 * Every privileged action is written here and none of them can be edited or
 * deleted from the dashboard. There is no write control on this page on purpose;
 * that absence is a property of the product, not an omission.
 */
export function AuditPage() {
  const query = useQueryState()
  const jobId = query.get('job_id', '') ?? ''
  const resource = query.get('resource', '') ?? ''
  const actorType = query.get('actor_type', '') ?? ''
  const page = normalizePage(query.getNumber('page', 1))
  const limit = DEFAULT_PAGE_SIZE
  const offset = (page - 1) * limit

  const events = useAsync<Paginated<AuditEvent>>(
    (signal) => api.listAudit({ job_id: jobId, resource, limit, offset }, { signal }),
    [jobId, resource, limit, offset],
  )

  const filtersActive = jobId !== '' || resource !== '' || actorType !== ''

  // `actor_type` has no documented parameter on the endpoint, so it filters the
  // loaded page on the client. Noted as a contract gap in the README.
  const visible = (events.data?.items ?? []).filter(
    (event) => actorType === '' || event.actor_type === actorType,
  )

  return (
    <>
      <PageHeader
        title="Audit"
        description="Append-only record of privileged actions. Read-only: the dashboard cannot edit or delete an audit event."
        actions={
          <button type="button" className="button" onClick={events.refetch}>
            Refresh
          </button>
        }
      />

      <Panel title="Filters">
        <div className="filter-bar">
          <label className="field">
            <span className="field__label">Job id</span>
            <input
              className="input"
              type="text"
              value={jobId}
              onChange={(event) => query.update({ job_id: event.target.value, page: 1 })}
              placeholder="job_01H…"
            />
          </label>
          <label className="field">
            <span className="field__label">Resource</span>
            <input
              className="input"
              type="text"
              value={resource}
              onChange={(event) => query.update({ resource: event.target.value, page: 1 })}
              placeholder="job / question / approval"
            />
          </label>
          <label className="field">
            <span className="field__label">Actor type</span>
            <select
              className="select"
              value={actorType}
              onChange={(event) => query.update({ actor_type: event.target.value, page: 1 })}
            >
              <option value="">All actor types</option>
              {['SYSTEM', 'AGENT', 'HUMAN', 'WEBHOOK', 'WORKER'].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            <span className="field__hint">
              Actor type filters the loaded page in the browser; the endpoint documents no such
              parameter.
            </span>
          </label>
          {filtersActive ? (
            <button
              type="button"
              className="button button--ghost"
              onClick={() => query.clear(['job_id', 'resource', 'actor_type', 'page'])}
            >
              Clear filters
            </button>
          ) : null}
        </div>
      </Panel>

      <Panel flush>
        <AsyncBoundary state={events} context="audit events" errorOverridesData>
          {events.data !== null ? (
            visible.length === 0 ? (
              <EmptyState
                title={filtersActive ? 'No audit event matches these filters' : 'No audit events yet'}
                description={
                  filtersActive
                    ? 'Widen or clear the filters to see more of the log.'
                    : 'Scout has not recorded a privileged action yet.'
                }
              />
            ) : (
              <div className="table-scroll">
                <table className="table">
                  <caption className="visually-hidden">Audit events</caption>
                  <thead>
                    <tr>
                      <th scope="col">When</th>
                      <th scope="col">Actor</th>
                      <th scope="col">Actor type</th>
                      <th scope="col">Action</th>
                      <th scope="col">Resource</th>
                      <th scope="col">Job</th>
                      <th scope="col">Metadata</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((event) => {
                      const actor = actorTypeMeta(event.actor_type)
                      return (
                        <tr key={event.id} className="table__row">
                          <td className="table__cell">
                            <RelativeTime value={event.at} />
                          </td>
                          <td className="table__cell">{event.actor}</td>
                          <td className="table__cell">
                            <Badge tone={actor.tone} description={actor.description}>
                              {event.actor_type}
                            </Badge>
                          </td>
                          <td className="table__cell">
                            <code>{event.action}</code>
                          </td>
                          <td className="table__cell">
                            <code>{event.resource}</code>
                          </td>
                          <td className="table__cell">
                            {event.job_id === null ? (
                              <span className="muted">—</span>
                            ) : (
                              <code>{event.job_id}</code>
                            )}
                          </td>
                          <td className="table__cell">
                            {Object.keys(event.metadata).length === 0 ? (
                              <span className="muted">—</span>
                            ) : (
                              <details>
                                <summary>Details</summary>
                                <pre>{JSON.stringify(event.metadata, null, 2)}</pre>
                              </details>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )
          ) : null}
        </AsyncBoundary>
      </Panel>

      {events.data !== null ? (
        <Pagination
          page={page}
          pageSize={limit}
          total={events.data.total}
          busy={events.refreshing}
          onPageChange={(next) => query.update({ page: next < 1 ? 1 : next })}
        />
      ) : null}
    </>
  )
}
