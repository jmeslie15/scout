import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { NotFoundPage } from './pages/NotFoundPage'
import { DashboardPage } from './pages/DashboardPage'
import { JobsPage } from './pages/JobsPage'
import { JobDetailPage } from './pages/JobDetailPage'
import { RepositoriesPage } from './pages/RepositoriesPage'
import { RepositoryDetailPage } from './pages/RepositoryDetailPage'
import { QuestionsPage } from './pages/QuestionsPage'
import { KnowledgePage } from './pages/KnowledgePage'
import { AgentsPage } from './pages/AgentsPage'
import { ToolsPage } from './pages/ToolsPage'
import { AuditPage } from './pages/AuditPage'

/**
 * Route table.
 *
 * Routing is HTML5-history based (`BrowserRouter`), so every path below is a
 * real URL that must resolve to `index.html` on refresh. The Vite dev server
 * does that via `appType: 'spa'`; a production static host needs the equivalent
 * rewrite (see apps/dashboard/README.md).
 *
 * `/` redirects to `/dashboard` with `replace` so the back button does not land
 * on a useless intermediate entry.
 */
export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/jobs" element={<JobsPage />} />
        <Route path="/jobs/:jobId" element={<JobDetailPage />} />
        <Route path="/repositories" element={<RepositoriesPage />} />
        <Route path="/repositories/:owner/:name" element={<RepositoryDetailPage />} />
        <Route path="/questions" element={<QuestionsPage />} />
        <Route path="/knowledge" element={<KnowledgePage />} />
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/tools" element={<ToolsPage />} />
        <Route path="/audit" element={<AuditPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}
