import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api, ApiError } from '../api/client'
import { useAsync } from '../api/hooks'
import type { ReadinessCheck, ReadinessResponse } from '../api/types'
import { Badge } from './Badge'
import { ReadinessContext } from './readinessContext'
import { RelativeTime } from './RelativeTime'

const NAV_ITEMS: readonly { to: string; label: string; description: string }[] = [
  { to: '/dashboard', label: 'Dashboard', description: 'Fleet-wide metrics and recent jobs' },
  { to: '/jobs', label: 'Jobs', description: 'Every verification job' },
  { to: '/repositories', label: 'Repositories', description: 'Watched repositories' },
  { to: '/questions', label: 'Questions', description: 'Open human escalations' },
  { to: '/knowledge', label: 'Knowledge', description: 'Engineering memory' },
  { to: '/agents', label: 'Agents', description: 'Agent roster and cost' },
  { to: '/tools', label: 'Tools', description: 'Deterministic tool roster' },
  { to: '/audit', label: 'Audit', description: 'Privileged-action log (read-only)' },
]

const READINESS_POLL_MS = 30_000

function summarizeChecks(ready: ReadinessResponse): string {
  const entries = Object.entries(ready.checks) as [string, ReadinessCheck][]
  const failing = entries.filter(([, check]) => !check.ok)
  if (failing.length === 0) return 'All dependencies report healthy.'
  return failing
    .map(([name, check]) => `${name}: ${check.detail ?? 'not ok'}`)
    .join(' · ')
}

function ReadinessIndicator({
  readiness,
  error,
  loading,
}: {
  readiness: ReadinessResponse | null
  error: ApiError | null
  loading: boolean
}) {
  if (loading && readiness === null && error === null) {
    return (
      <Badge tone="neutral" description="Querying GET /ready">
        CHECKING READINESS
      </Badge>
    )
  }

  if (error !== null || readiness === null) {
    return (
      <Badge
        tone="failed"
        description={error !== null ? error.displayMessage : 'The readiness endpoint did not respond.'}
      >
        READINESS UNKNOWN
      </Badge>
    )
  }

  const ready = readiness.status === 'ready'
  return (
    <Badge tone={ready ? 'verified' : 'human'} description={summarizeChecks(readiness)}>
      {ready ? 'API READY' : 'API DEGRADED'}
    </Badge>
  )
}

/** Banner shown on every page when the API cannot be reached at all. */
function UnreachableBanner({ error }: { error: ApiError }) {
  return (
    <div className="banner banner--error" role="alert">
      <strong>Scout API unreachable.</strong>{' '}
      {error.kind === 'timeout'
        ? 'The readiness check timed out — the API process may be hung or overloaded.'
        : 'No response from the readiness endpoint.'}{' '}
      <span className="muted">
        Checked <RelativeTime value={new Date().toISOString()} intervalMs={15_000} />
      </span>
    </div>
  )
}

/** Banner shown when the API answers but reports a dependency problem. */
function DegradedBanner({ readiness }: { readiness: ReadinessResponse }) {
  const entries = Object.entries(readiness.checks) as [string, ReadinessCheck][]
  const failing = entries.filter(([, check]) => !check.ok)
  if (failing.length === 0) return null
  return (
    <div className="banner banner--warning" role="status">
      <strong>Scout API is degraded.</strong> Verification may not run to completion.{' '}
      <ul className="banner__list">
        {failing.map(([name, check]) => (
          <li key={name}>
            <code>{name}</code>: {check.detail ?? 'not ok'}
          </li>
        ))}
      </ul>
    </div>
  )
}

function PageTitle() {
  const location = useLocation()
  const match = NAV_ITEMS.find(
    (item) => location.pathname === item.to || location.pathname.startsWith(`${item.to}/`),
  )
  const title =
    match?.label ??
    (location.pathname.startsWith('/jobs/') ? 'Job detail' : 'Scout operator dashboard')
  return <h1 className="app-header__title">{title}</h1>
}

export interface AppShellProps {
  /**
   * Readiness supplied by the caller (used by tests and by pages that already
   * fetched it). When omitted, the shell fetches it itself and polls.
   */
  readiness?: ReadinessResponse | null
  readinessError?: ApiError | null
}

/**
 * Application chrome: left navigation, header with the API readiness indicator,
 * and a banner when the API is unreachable or degraded.
 *
 * The banner is rendered above the routed page so it cannot be missed, and it
 * survives navigation — an operator on `/jobs` sees the same outage notice as an
 * operator on `/audit`.
 */
export function AppShell({ readiness: readinessProp, readinessError }: AppShellProps = {}) {
  const owned = useAsync<ReadinessResponse>(
    (signal) => api.ready({ signal, timeoutMs: 8000 }),
    [],
    { pollMs: READINESS_POLL_MS, enabled: readinessProp === undefined && readinessError === undefined },
  )

  const readiness = readinessProp !== undefined ? readinessProp : owned.data
  const error = readinessError !== undefined ? readinessError : owned.error

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <nav className="app-nav" aria-label="Primary">
        <div className="app-nav__brand">
          <span className="app-nav__logo" aria-hidden="true">
            S
          </span>
          <span className="app-nav__brand-text">
            <strong>Scout</strong>
            <span className="muted">operator dashboard</span>
          </span>
        </div>
        <ul className="app-nav__list">
          {NAV_ITEMS.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                className={({ isActive }) =>
                  isActive ? 'app-nav__link app-nav__link--active' : 'app-nav__link'
                }
                title={item.description}
              >
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
        <p className="app-nav__footer muted">
          AI investigates and recommends. Deterministic systems verify. Humans authorize.
        </p>
      </nav>

      <div className="app-main">
        <header className="app-header">
          <PageTitle />
          <div className="app-header__status">
            <span className="muted app-header__status-label">Readiness</span>
            <ReadinessIndicator
              readiness={readiness}
              error={error ?? null}
              loading={owned.status === 'loading'}
            />
          </div>
        </header>

        {error !== null && error !== undefined ? (
          error.isUnreachable ? (
            <UnreachableBanner error={error} />
          ) : (
            <div className="banner banner--warning" role="status">
              <strong>Readiness check failed.</strong> {error.displayMessage}
            </div>
          )
        ) : null}

        {readiness !== null && readiness !== undefined && readiness.status !== 'ready' ? (
          <DegradedBanner readiness={readiness} />
        ) : null}

        <main className="app-content" id="main-content">
          <ReadinessContext.Provider
            value={{
              readiness: readiness ?? null,
              error: error ?? null,
              loading: owned.status === 'loading',
              refetch: owned.refetch,
            }}
          >
            <Outlet />
          </ReadinessContext.Provider>
        </main>
      </div>
    </div>
  )
}
