import { Link, useLocation } from 'react-router-dom'
import { PageHeader, Panel } from '../components/Layout'

/** 404 for an unmatched route. Rendered inside the shell so navigation survives. */
export function NotFoundPage() {
  const location = useLocation()
  return (
    <>
      <PageHeader
        title="Page not found"
        description="This route is not part of the Scout operator dashboard."
      />
      <Panel title="Nothing at this path">
        <p>
          No route matches <code>{location.pathname}</code>.
        </p>
        <p>
          <Link to="/dashboard">Go to the dashboard</Link>
        </p>
      </Panel>
    </>
  )
}
