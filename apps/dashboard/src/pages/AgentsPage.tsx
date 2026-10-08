import { api } from '../api/client'
import { useAsync } from '../api/hooks'
import type { AgentSummary, ItemsResponse } from '../api/types'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { Badge } from '../components/Badge'
import { EmptyState } from '../components/EmptyState'
import { PageHeader, Panel, StatCard } from '../components/Layout'
import { Table, TBody, TD, TH, THead, TR } from '../components/Table'
import { formatCompactNumber, formatDuration, formatUsd } from '../lib/badges'

/**
 * Agent roster.
 *
 * `AgentRole` is a closed set of seven roles and agents do not spawn other
 * agents, so this list is expected to be short and stable. Capabilities are
 * shown because capability is granted from above by the orchestrator — this
 * column is the operator's evidence that an agent was not given more reach than
 * its role needs.
 *
 * Token and cost totals come from `totals`; per-run detail lives on the job
 * detail page.
 */
export function AgentsPage() {
  const agents = useAsync<ItemsResponse<AgentSummary>>((signal) => api.listAgents({ signal }), [])

  return (
    <>
      <PageHeader
        title="Agents"
        description="The fixed set of reasoning agents. Agents do not spawn other agents; the orchestrator schedules these roles explicitly."
        actions={
          <button type="button" className="button" onClick={agents.refetch}>
            Refresh
          </button>
        }
      />

      <AsyncBoundary state={agents} context="the agent roster" errorOverridesData>
        {agents.data !== null ? <AgentRoster agents={agents.data.items} /> : null}
      </AsyncBoundary>
    </>
  )
}

function AgentRoster({ agents }: { agents: readonly AgentSummary[] }) {
  if (agents.length === 0) {
    return (
      <Panel>
        <EmptyState
          title="No agents reported"
          description="The agent roster is empty. Either no agent has run yet, or the agents endpoint returned nothing."
        />
      </Panel>
    )
  }

  const totalRuns = agents.reduce((sum, agent) => sum + agent.runs, 0)
  const totalTokens = agents.reduce(
    (sum, agent) => sum + agent.totals.input_tokens + agent.totals.output_tokens,
    0,
  )
  const totalCost = agents.reduce((sum, agent) => sum + agent.totals.estimated_cost_usd, 0)

  return (
    <>
      <div className="stat-grid stat-grid--compact">
        <StatCard label="Roles" value={agents.length} hint="fixed set of seven" />
        <StatCard label="Total runs" value={totalRuns} />
        <StatCard label="Total tokens" value={formatCompactNumber(totalTokens)} />
        <StatCard label="Estimated cost" value={formatUsd(totalCost)} />
      </div>

      <Panel title="Roster" flush>
        <Table caption="Agent roster">
          <THead>
            <TR>
              <TH>Role</TH>
              <TH>Description</TH>
              <TH>Capabilities</TH>
              <TH>Prompt versions</TH>
              <TH numeric>Runs</TH>
              <TH numeric>Avg latency</TH>
              <TH numeric>Input tokens</TH>
              <TH numeric>Output tokens</TH>
              <TH numeric>Cost</TH>
            </TR>
          </THead>
          <TBody>
            {agents.map((agent) => (
              <TR key={agent.role}>
                <TD>
                  <code>{agent.role}</code>
                </TD>
                <TD>{agent.description}</TD>
                <TD>
                  {agent.capabilities.length === 0 ? (
                    <span className="muted">none granted</span>
                  ) : (
                    <ul className="tag-list">
                      {agent.capabilities.map((capability) => (
                        <li key={capability} className="tag">
                          <code>{capability}</code>
                        </li>
                      ))}
                    </ul>
                  )}
                </TD>
                <TD>
                  {agent.prompt_versions.length === 0 ? (
                    <span className="muted">—</span>
                  ) : (
                    <ul className="compact-list">
                      {agent.prompt_versions.map((version) => (
                        <li key={version}>
                          <code>{version}</code>
                        </li>
                      ))}
                    </ul>
                  )}
                </TD>
                <TD numeric>{agent.runs}</TD>
                <TD numeric>{formatDuration(agent.avg_latency_seconds)}</TD>
                <TD numeric>{formatCompactNumber(agent.totals.input_tokens)}</TD>
                <TD numeric>{formatCompactNumber(agent.totals.output_tokens)}</TD>
                <TD numeric>{formatUsd(agent.totals.estimated_cost_usd)}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </Panel>

      <Panel title="Why this matters">
        <p className="muted">
          <Badge tone="ai">AI</Badge> Agent output is reasoning, not evidence. A finding reaches{' '}
          <code>is_verified_fact: true</code> only when its provenance is DETERMINISTIC, at least one
          cited reference is MACHINE strength, and its confidence was not capped — which means no agent
          can make its own conclusion a verified fact.
        </p>
      </Panel>
    </>
  )
}
