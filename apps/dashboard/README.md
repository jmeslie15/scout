# Scout operator dashboard

The operator-facing UI for Scout. It is a read-mostly view over the control-plane
API: job timeline, findings, **evidence**, open questions, approvals, and the
read-only audit log.

> **AI investigates and recommends. Deterministic systems verify. Humans authorize.**

This package holds no control-plane credentials. It talks to the API as an
authenticated operator would, and the only write paths it exposes are the human
ones: record an approval decision, answer a question, add a knowledge document,
cancel or retry a job.

---

## Requirements

| Tool | Version | Notes |
|---|---|---|
| Node.js | 20 LTS | 20.20.2 verified. The system default may be 16, which Vite 5 does not support. |
| pnpm | 9 | `corepack enable && corepack prepare pnpm@9 --activate` |

React 18 · TypeScript 5 (strict) · Vite 5 · vitest + Testing Library.

## Run it

```bash
# Node 20 must be first on PATH; the system default may be v16.
export PATH="$HOME/.nvm/versions/node/v20.20.2/bin:$PATH"
node --version    # v20.20.2

cd apps/dashboard
pnpm install

cp .env.example .env    # optional; the default is http://localhost:8000
pnpm dev                # http://localhost:5173
```

The API is expected at `http://localhost:8000` (`make api` from the repository
root). If it is not running the dashboard still loads: the shell shows an
"Scout API unreachable" banner and every page renders an error state with a
Retry button and the HTTP status.

Other scripts:

```bash
pnpm typecheck   # tsc --noEmit
pnpm lint        # eslint (flat config)
pnpm test        # vitest run
pnpm build       # tsc --noEmit && vite build -> dist/
pnpm preview     # serve dist/ locally
```

## API base URL

`VITE_SCOUT_API_URL` is the **only** place the API host is named. It is read at
build time by Vite and inlined via `import.meta.env` in `src/api/client.ts`
(`apiBaseUrl()`); the localhost default exists so a fresh clone works against a
local API with no `.env`.

In development the app never calls that origin directly. `vite.config.ts`
proxies `/api`, `/health`, and `/ready` to `VITE_SCOUT_API_URL`, so the browser
only ever talks to `http://localhost:5173` and **CORS never enters the picture**.
The request paths are unchanged — `GET /api/v1/jobs` on port 5173 becomes
`GET /api/v1/jobs` on port 8000 — so the client needs no dev-only code path.

```
browser  ->  http://localhost:5173/api/v1/jobs  ->  proxy  ->  http://localhost:8000/api/v1/jobs
```

For a production build, set `VITE_SCOUT_API_URL` to the real API origin and
rebuild. The browser then fetches that origin directly, so **that origin must
allow the dashboard origin via CORS**. Alternatively, serve the API under the
same origin behind a reverse proxy and leave the variable relative.

## Routing, and the production rewrite

Routing uses **`react-router-dom` v6** with the HTML5 history API
(`BrowserRouter`) rather than a hand-rolled router. The reason is that the brief
requires ten routes including nested and parameterised ones
(`/jobs/:id`, `/repositories/:owner/:name`) to work **on refresh**; v6 is a
single small dependency that solves exactly that and is the thing a reviewer
already knows how to read. If you would rather have no router dependency, the
alternative in the brief — `window.location.pathname` with a `Link` component
and a `useRoute` hook — would replace `src/App.tsx` and nothing else: no page
imports the router's data APIs (`useParams`, `useSearchParams`, `NavLink`), so
the change stays contained.

Every path in the app is a real URL:

```
/dashboard
/jobs                      /jobs/:jobId
/repositories              /repositories/:owner/:name
/questions
/knowledge
/agents
/tools
/audit
/                          -> redirects to /dashboard
```

Because these are real paths, the server must fall back to `index.html` for
unknown paths. In development the Vite config sets **`appType: 'spa'`**, which
does that already; `curl http://localhost:5173/jobs/job-1` returns the app HTML,
not a 404.

**A production static host needs the same rewrite.** Examples:

| Host | Rule |
|---|---|
| nginx | `location / { try_files $uri $uri/ /index.html; }` |
| Caddy | `try_files {path} /index.html` |
| Netlify / Cloudflare Pages | `/* /index.html 200` |
| S3 + CloudFront | error document `index.html` for 403/404, or a CloudFront function rewriting to `/index.html` |

Without it, refreshing `/jobs` returns the host's 404 and an operator loses the
page they were reading.

## What the dashboard does that matters

### Evidence is the product

Every finding, everywhere it is rendered, expands to show:

- its **provenance** — `DETERMINISTIC`, `AI_ASSISTED`, `AI_ONLY`, `UNSUPPORTED`;
- its **evidence references**, each labelled with its **strength** — `MACHINE`,
  `INSPECTION`, `AUTHORITATIVE`, `INFERENCE` — plus kind, reference id, summary,
  file/line range, excerpt, artifact id, and url;
- whether it is a **verified fact** (`is_verified_fact: true`) or merely
  AI-suggested. The two render with different wording, different left border,
  and different `data-*` attributes, so the distinction does not depend on
  colour;
- whether its **confidence was capped** (`confidence_capped: true`) and whether
  it **requires human review** (`requires_human: true`).

A verified fact requires all three of: provenance `DETERMINISTIC`, at least one
`MACHINE`-strength reference, and an uncapped confidence. The UI says exactly
that in the finding's badge `title`, and the `EvidenceStrengthLegend` component
repeats it above every findings list.

### The invalidated-approval warning (spec section 74)

An approval is bound to a commit SHA. When the pull request moves, the approval
is invalidated rather than deleted. The job detail page renders a full-width
`role="alert"` warning above everything else whenever an approval exists and
`current_approval_valid` is `false`, naming the decision, who made it, the commit
it covered, the commit now under review, when it was invalidated, and why. It is
not dismissible and it is not a tooltip.

### Human authorization

`POST /api/v1/jobs/{id}/approve` is driven by a real form with a required
`decided_by`, an optional comment, and **one button per decision** —
`APPROVE`, `REQUEST_CHANGES`, `REJECT`. There is no default decision and no
dropdown that could be submitted by muscle memory, because the dangerous action
must be deliberate. `decided_by` is never persisted client-side.

### Polling

The job detail page polls `GET /api/v1/jobs/{id}` every **5 seconds** while the
job can still move, and **stops** when it cannot. "Can still move" excludes
`COMPLETED` and `CANCELLED` but **includes `FAILED`**, because `FAILED` is a
resting state rather than a tombstone — an operator may re-queue it. The
current polling state is announced in an `aria-live` region.

## Presentation decisions

### Job status colour mapping

The palette separates four semantically different situations, and no two of them
share a tone. The full mapping, with the reasoning, is in
`src/lib/badges.ts`; the summary is:

| Tone | Statuses | Meaning |
|---|---|---|
| `active` (blue, pulsing dot) | `RECEIVED`, `QUEUED`, `PREPARING`, `ANALYZING`, `REVIEWING`, `SECURITY_SCANNING`, `TESTING`, `FEATURE_VERIFYING`, `INVESTIGATING` | Scout is working; nothing for a human to do. The pulse marks it as a value that will change on its own. |
| `review` (violet) | `READY_FOR_REVIEW` | Verification finished cleanly; on a human's desk for a decision. |
| `human` (amber) | `WAITING_FOR_HUMAN` | Blocked on a human *answer*, not a decision. A different queue from `review`. |
| `verified` (green) | `COMPLETED` | Terminal and authorized. |
| `failed` (red) | `FAILED` | Resting state; retry is available. |
| `cancelled` (grey) | `CANCELLED` | Terminal; no action needed. |

`active` and `review` are deliberately adjacent hues because both mean "in the
system's hands"; `human` is amber so an operator scanning a list can pick out
the jobs that are actually waiting on a person.

### Other decisions

- **Plain `fetch`, `useState`/`useEffect`, and CSS.** No component library, no
  state manager, no data-fetching library, no CSS framework. Scout's policy is
  "avoid framework magic" and "do not introduce infrastructure before it is
  necessary"; `src/api/hooks.ts` is one ~100-line `useAsync` hook that owns
  loading/error/data, cancellation on unmount, and an optional poll interval,
  and it is the only data-fetching abstraction in the app.
- **Hand-written CSS in one file** (`src/styles.css`), organised by component,
  with semantic tone custom properties. Dark mode is a
  `prefers-color-scheme` block that redefines those properties, so no component
  needs a second rule. Tailwind was skipped deliberately: a build step and a
  config to review would have bought nothing here.
- **Filters live in the URL** (`src/lib/useQueryState.ts`), because operators
  paste links to each other. Updates use `replace` so the back button stays
  useful.
- **Tables are semantic HTML with thin wrappers** (`src/components/Table.tsx`),
  not a `<DataTable columns={…}>` abstraction. Column configs buy nothing at
  this size and make the markup harder to review.
- **Colour is never the only signal.** Every badge also carries its text label;
  the tests assert this.

## Testing

`pnpm test` runs seven suites:

| Suite | What it protects |
|---|---|
| `src/api/client.test.ts` | Success, 404 raising `ApiError` with `status === 404`, a 500 staying distinct from a 404, FastAPI list-shaped `detail`, empty body → `null`, timeout via `AbortController`, network failure, query-parameter omission, POST body/headers. |
| `src/lib/badges.test.ts` | Every status/severity/risk/provenance/strength mapping is total, and the six job-status situations have six distinct tones. |
| `src/lib/styles.test.ts` | Every tone the code can produce has a `.badge--<tone>` rule in `src/styles.css`, and none of those rules forgot its `background`. Catches the "unstyled badge" failure that survives a type check. |
| `src/lib/time.test.ts` | Relative time units, pluralisation, future timestamps, unparseable input. |
| `src/components/RelativeTime.test.tsx` | The absolute time is in `title` and the ISO value is in `datetime`. |
| `src/pages/JobDetailPage.test.tsx` | The invalidated-approval warning appears exactly when it should, and an `AI_ONLY` finding is visibly and textually distinct from a verified fact. |
| `src/App.test.tsx` | Every route in the brief renders without throwing, `/` redirects, an unknown route shows a not-found page, and the nav is present everywhere. |

Coverage is not a goal; the tests target logic whose breakage would be
embarrassing (a 404 reported as a server fault, an AI-only finding looking
verified) rather than chasing a percentage.

## API contract gaps

The dashboard does not invent API fields. Where the contract given to this
workstream was silent or narrower than the UI needs, the gap is recorded here and
the UI renders what exists.

1. **`GET /api/v1/jobs/{id}/report` has no documented response shape.** The
   contract says `Report | null` without defining `Report`. The dashboard
   consumes `recommendation`, `verification_status`, `summary`, `criteria`,
   `findings`, `unverified_areas`, `evidence_backed_finding_count`, and
   `generated_at` defensively — every field is optional, and an unrecognised
   shape degrades to "not available" rather than throwing. **Feature
   verification** is rendered from `report.criteria`
   (`criterion`, `procedure`, `expected_result`, `actual_result`, `status`,
   `evidence`); if the server names that field differently, the panel shows an
   explicit empty state saying the report did not include criteria. This is the
   one place where a server-side shape decision determines what the dashboard
   can show.

2. **No category on `ToolRun`.** The contract exposes `tool` and a free-form
   `summary` but no category, so the **Tests**, **Security**, and
   **Dependencies** panels are derived by matching tool names
   (`deriveToolSections` in `src/components/JobPanels.tsx`). Name matching is
   brittle; a `category` field on the tool run, or a tool-catalogue lookup by
   name, would be strictly better.

3. **No filters on `GET /api/v1/repositories`.** The Repositories page search
   therefore filters the loaded list in the browser. Fine for tens of
   repositories, not for thousands.

4. **No `actor_type` parameter on `GET /api/v1/audit`.** The Audit page filters
   the loaded page in the browser by actor type, which means a filtered view can
   show fewer rows than `total` implies. The UI says so on the filter itself.

5. **No authority parameter on `GET /api/v1/knowledge`.** Same situation: the
   authority filter is client-side over the fetched page.

6. **`GET /api/v1/repositories/{owner}/{name}` field name for recent jobs is not
   specified.** The dashboard reads `recent_jobs`. If the server uses another
   name, the page renders an explicit gap notice instead of silently showing
   nothing.

7. **No findings endpoint or query parameters.** Findings are filtered and
   sorted client-side from `JobDetail.findings`. Acceptable because a single
   job's findings are bounded.

8. **No pagination parameters on `GET /api/v1/agents` or `/tools`.** Both
   endpoints are assumed to return the complete roster, which is reasonable for
   a fixed set of seven agent roles.

## Layout

```
apps/dashboard/
  index.html
  vite.config.ts        dev server: port 5173, SPA fallback, /api + /health + /ready proxy
  vitest.config.ts
  eslint.config.js      flat config
  tsconfig.json         strict + noUncheckedIndexedAccess, no `any`
  .env.example          documents VITE_SCOUT_API_URL
  src/
    api/
      client.ts         request<T>, ApiError, one typed function per endpoint
      types.ts          every wire shape; no `any`
      hooks.ts          useAsync: loading/error/data, cancellation, polling
    components/         AppShell, badges, EvidenceList, FindingCard, StageTimeline, Table, Tabs, …
    lib/
      badges.ts         the total presentation mappings and formatters
      time.ts           relative/absolute time, pure and testable
      pagination.ts     offset/limit <-> page conversion
      useQueryState.ts  URL-backed filters
    pages/              one file per route
    test/fixtures.ts    typed fixtures used by the tests
    styles.css          all styling, incl. prefers-color-scheme dark mode
```

## Not done

- **No authentication.** The dashboard sends no credentials. The API is assumed
  to be reachable only from the operator network, and `decided_by` is typed by
  hand. An authenticated operator identity would be the honest next step and
  would replace the free-text field.
- **No artifact content viewer.** Artifacts are listed with their kind, size,
  content type, and SHA-256, but the bytes are not fetched — the contract has no
  artifact-content endpoint. The SHA-256 is shown so a human can verify a
  download out of band.
- **No live updates.** There is no SSE or WebSocket. Polling is deliberate and
  bounded; if the API later exposes a stream, the polling interval in
  `JobDetailPage` is the thing to replace.
