import { api } from '../api/client'
import { useAsync } from '../api/hooks'
import type { ItemsResponse, ToolSummary } from '../api/types'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { Badge } from '../components/Badge'
import { EmptyState } from '../components/EmptyState'
import { PageHeader, Panel, StatCard } from '../components/Layout'
import { Table, TBody, TD, TH, THead, TR } from '../components/Table'
import { formatDuration } from '../lib/badges'

/**
 * Deterministic tool roster.
 *
 * This is the other half of the evidence story: these are the components whose
 * output can reach MACHINE strength. `applicable` is shown as its own column
 * because "did not run" is only meaningful next to "was it applicable" — a tool
 * that was not applicable is not a gap in coverage.
 */
export function ToolsPage() {
  const tools = useAsync<ItemsResponse<ToolSummary>>((signal) => api.listTools({ signal }), [])

  return (
    <>
      <PageHeader
        title="Tools"
        description="Deterministic checks executed inside the sandbox. Tool output is the strongest evidence Scout can hold: MACHINE strength."
        actions={
          <button type="button" className="button" onClick={tools.refetch}>
            Refresh
          </button>
        }
      />

      <AsyncBoundary state={tools} context="the tool roster" errorOverridesData>
        {tools.data !== null ? <ToolRoster tools={tools.data.items} /> : null}
      </AsyncBoundary>
    </>
  )
}

function ToolRoster({ tools }: { tools: readonly ToolSummary[] }) {
  if (tools.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No tools reported"
          description="The tool roster is empty. Either no tool has run yet, or the tools endpoint returned nothing."
        />
      </Panel>
    )
  }

  const totalRuns = tools.reduce((sum, tool) => sum + tool.runs, 0)
  const totalFailures = tools.reduce((sum, tool) => sum + tool.failures, 0)
  const failureRate = totalRuns > 0 ? Math.round((totalFailures / totalRuns) * 100) : null

  return (
    <>
      <div className="stat-grid stat-grid--compact">
        <StatCard label="Tools" value={tools.length} />
        <StatCard
          label="Applicable"
          value={tools.filter((tool) => tool.applicable).length}
          hint={`${tools.filter((tool) => !tool.applicable).length} not applicable`}
        />
        <StatCard label="Total runs" value={totalRuns} />
        <StatCard
          label="Failures"
          value={totalFailures}
          tone={totalFailures > 0 ? 'warn' : 'good'}
          hint={failureRate === null ? 'no runs yet' : `${failureRate}% of runs`}
        />
      </div>

      <Panel title="Roster" flush>
        <Table caption="Deterministic tool roster">
          <THead>
            <TR>
              <TH>Tool</TH>
              <TH>Category</TH>
              <TH>Description</TH>
              <TH>Capability</TH>
              <TH>Applicable</TH>
              <TH numeric>Runs</TH>
              <TH numeric>Failures</TH>
              <TH numeric>Avg duration</TH>
            </TR>
          </THead>
          <TBody>
            {tools.map((tool) => (
              <TR key={tool.name}>
                <TD>
                  <code>{tool.name}</code>
                </TD>
                <TD>{tool.category}</TD>
                <TD>{tool.description}</TD>
                <TD>
                  <code>{tool.capability}</code>
                </TD>
                <TD>
                  {tool.applicable ? (
                    <span className="marker marker--verified">Applicable</span>
                  ) : (
                    <span className="marker marker--muted" title="Not applicable to this change; not a coverage gap.">
                      Not applicable
                    </span>
                  )}
                </TD>
                <TD numeric>{tool.runs}</TD>
                <TD numeric>
                  {tool.failures > 0 ? (
                    <span className="text-error">{tool.failures}</span>
                  ) : (
                    <span className="muted">0</span>
                  )}
                </TD>
                <TD numeric>{formatDuration(tool.avg_duration_seconds)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </Panel>

      <Panel title="Failure counts need context">
        <p className="muted">
          A failure count is not by itself a quality signal: a linter that fails because it found a real
          problem has done its job. Read a specific run's outcome, exit code, and{' '}
          <Badge tone="failed">infrastructure_failure</Badge> flag on the job detail page before drawing
          a conclusion about a tool.
        </p>
      </Panel>
    </>
  )
}
