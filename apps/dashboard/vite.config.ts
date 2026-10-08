import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Vite configuration for the Scout operator dashboard.
 *
 * Routing decision: the dashboard uses `react-router-dom` v6 with the HTML5
 * history API (`BrowserRouter`). That means every route is a real path, so the
 * dev server (and any production static host) must rewrite unknown paths to
 * `/index.html`. `appType: 'spa'` is what enables that fallback here; a static
 * host needs the equivalent rule (see apps/dashboard/README.md).
 *
 * Proxy decision: in dev we proxy `/api`, `/health`, and `/ready` to
 * `VITE_SCOUT_API_URL` so the browser only ever talks to one origin and CORS
 * never enters the picture. Requests still carry the full path, so the client
 * can keep using `VITE_SCOUT_API_URL` as the base URL with no dev-only branch.
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = env.VITE_SCOUT_API_URL || 'http://localhost:8000'

  return {
    plugins: [react()],
    appType: 'spa',
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        '/api': { target: apiTarget, changeOrigin: true },
        '/health': { target: apiTarget, changeOrigin: true },
        '/ready': { target: apiTarget, changeOrigin: true },
      },
    },
    preview: {
      port: 5173,
      strictPort: true,
    },
    build: {
      outDir: 'dist',
      sourcemap: true,
    },
  }
})
