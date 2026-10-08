import { useState } from 'react'
import { ApiError, api } from '../api/client'
import type { ApprovalDecision, ApprovalRequest, JobDetail } from '../api/types'
import { approvalDecisionMeta, recommendationMeta } from '../lib/badges'
import { Badge } from './Badge'
import { RelativeTime } from './RelativeTime'

export interface ApprovalControlProps {
  job: JobDetail
  /** Called with the updated job after a successful POST. */
  onDecided: (job: JobDetail) => void
}

const DECISION_HINTS: Record<ApprovalDecision, string> = {
  APPROVE:
    'Authorizes this exact head commit. The approval is bound to the commit SHA and is invalidated automatically if the PR moves.',
  REQUEST_CHANGES: 'Records that the author must change something before this can be authorized.',
  REJECT: 'Records that this change is not going to be authorized in its current form.',
}

/**
 * The human authorization control.
 *
 * This is the only write path in the dashboard that grants authority, so it is
 * built as a real form: labelled inputs, `required`, `minLength`, native
 * validation, and one submit button per decision (no dropdown that defaults to
 * APPROVE, because the dangerous action must require a deliberate click).
 *
 * `decided_by` is required by the API and by Scout's policy — an approval with
 * no attributable human would violate "humans authorize". The value is not
 * persisted by the dashboard; it must be re-entered, which keeps the record
 * honest about who clicked.
 */
export function ApprovalControl({ job, onDecided }: ApprovalControlProps) {
  const [decidedBy, setDecidedBy] = useState('')
  const [comment, setComment] = useState('')
  const [pending, setPending] = useState<ApprovalDecision | null>(null)
  const [error, setError] = useState<ApiError | Error | null>(null)
  const [success, setSuccess] = useState<ApprovalDecision | null>(null)

  const submit = async (decision: ApprovalDecision): Promise<void> => {
    setError(null)
    setSuccess(null)

    const trimmedBy = decidedBy.trim()
    if (trimmedBy === '') {
      setError(new Error('A decision must be attributable to a human: enter who is deciding.'))
      return
    }

    const payload: ApprovalRequest = {
      decision,
      decided_by: trimmedBy,
      comment: comment.trim(),
    }

    setPending(decision)
    try {
      const updated = await api.approveJob(job.id, payload)
      onDecided(updated)
      setSuccess(decision)
      setComment('')
    } catch (caught) {
      setError(caught instanceof Error ? caught : new Error(String(caught)))
    } finally {
      setPending(null)
    }
  }

  const decisions: readonly ApprovalDecision[] = ['APPROVE', 'REQUEST_CHANGES', 'REJECT']

  return (
    <section className="approval-control" aria-labelledby="approval-control-heading">
      <header className="approval-control__header">
        <h3 id="approval-control-heading" className="panel__title">
          Human authorization
        </h3>
        <p className="panel__description muted">
          Scout recommends; a human authorizes. This decision is bound to commit{' '}
          <code>{job.head_sha.slice(0, 12)}</code>.
        </p>
      </header>

      {job.recommendation !== null && job.recommendation !== '' ? (
        <p className="approval-control__recommendation">
          <span className="muted">Scout recommends: </span>
          <Badge
            tone={recommendationMeta(job.recommendation).tone}
            description={recommendationMeta(job.recommendation).description}
          >
            {recommendationMeta(job.recommendation).label}
          </Badge>
        </p>
      ) : null}

      <form
        className="approval-control__form"
        onSubmit={(event) => {
          event.preventDefault()
        }}
      >
        <label className="field" htmlFor="approval-decided-by">
          <span className="field__label">
            Decided by <span className="required-marker">(required)</span>
          </span>
          <input
            id="approval-decided-by"
            name="decided_by"
            className="input"
            type="text"
            value={decidedBy}
            required
            minLength={1}
            autoComplete="name"
            placeholder="you@edrevel.ai"
            onChange={(event) => setDecidedBy(event.target.value)}
            aria-describedby="approval-decided-by-hint"
          />
          <span className="field__hint" id="approval-decided-by-hint">
            Recorded in the audit log as the authorizing human.
          </span>
        </label>

        <label className="field" htmlFor="approval-comment">
          <span className="field__label">Comment</span>
          <textarea
            id="approval-comment"
            name="comment"
            className="textarea"
            rows={3}
            value={comment}
            placeholder="Why this decision, and any condition attached to it."
            onChange={(event) => setComment(event.target.value)}
          />
        </label>

        <div className="approval-control__actions">
          {decisions.map((decision) => {
            const meta = approvalDecisionMeta(decision)
            const isPending = pending === decision
            return (
              <div key={decision} className="approval-control__action">
                <button
                  type="button"
                  className={`button button--decision button--decision-${decision.toLowerCase()}`}
                  disabled={pending !== null}
                  onClick={() => {
                    void submit(decision)
                  }}
                  title={DECISION_HINTS[decision]}
                >
                  {isPending ? 'Submitting…' : meta.label}
                </button>
                <span className="approval-control__action-hint muted">{DECISION_HINTS[decision]}</span>
              </div>
            )
          })}
        </div>
      </form>

      <div aria-live="polite" className="approval-control__feedback">
        {error !== null ? (
          <p className="form-error" role="alert">
            <strong>Decision not recorded.</strong>{' '}
            {error instanceof ApiError ? error.displayMessage : error.message}
            {error instanceof ApiError && error.status === 0
              ? ' Check that the API is running, then try again.'
              : ''}
          </p>
        ) : null}
        {success !== null ? (
          <p className="form-success">
            Recorded <strong>{approvalDecisionMeta(success).label}</strong>. The job record below has
            been refreshed.
          </p>
        ) : null}
        {pending !== null ? <p className="muted">Submitting {pending.replace(/_/g, ' ')}…</p> : null}
      </div>
    </section>
  )
}

export interface ApprovalHistoryProps {
  job: JobDetail
}

/** Recorded human decisions, newest first, with invalidation state. */
export function ApprovalHistory({ job }: ApprovalHistoryProps) {
  if (job.approvals.length === 0) {
    return <p className="muted">No human decision has been recorded for this job yet.</p>
  }

  const approvals = [...job.approvals].sort((a, b) =>
    (b.decided_at ?? '').localeCompare(a.decided_at ?? ''),
  )

  return (
    <ul className="approval-history">
      {approvals.map((approval) => {
        const meta = approvalDecisionMeta(approval.decision)
        const invalidated = approval.invalidated_at !== null || !approval.is_valid
        return (
          <li
            key={approval.id}
            className={`approval-history__item${invalidated ? ' approval-history__item--invalidated' : ''}`}
          >
            <div className="approval-history__head">
              <Badge tone={meta.tone} description={meta.description}>
                {meta.label}
              </Badge>
              <span className="approval-history__who">{approval.decided_by}</span>
              <RelativeTime value={approval.decided_at} />
              {invalidated ? (
                <span className="marker marker--invalidated">INVALIDATED</span>
              ) : (
                <span className="marker marker--verified">CURRENT</span>
              )}
            </div>
            {approval.comment !== '' ? (
              <p className="approval-history__comment">{approval.comment}</p>
            ) : null}
            <p className="approval-history__commit muted">
              bound to commit <code>{approval.commit_sha.slice(0, 12)}</code>
              {invalidated && approval.invalidated_at !== null ? (
                <>
                  {' · '}invalidated <RelativeTime value={approval.invalidated_at} />
                  {approval.invalidated_reason !== null && approval.invalidated_reason !== ''
                    ? `: ${approval.invalidated_reason}`
                    : ''}
                </>
              ) : null}
            </p>
          </li>
        )
      })}
    </ul>
  )
}

export interface ApprovalValidityWarningProps {
  job: JobDetail
}

/**
 * The invalidated-approval warning (spec section 74).
 *
 * It is rendered whenever the job carries an approval whose validity does not
 * cover the current commit. It is deliberately loud and unmissable: a full-width
 * alert above the stage timeline, naming the commit the approval covered, the
 * commit now under review, and the reason the API gave. The entire point is that
 * an operator must never mistake a stale authorization for a current one.
 */
export function ApprovalValidityWarning({ job }: ApprovalValidityWarningProps) {
  const invalidated = job.approvals.filter(
    (approval) => approval.invalidated_at !== null || !approval.is_valid,
  )
  const hasApproval = job.approvals.length > 0

  if (job.current_approval_valid || !hasApproval) return null

  const latest = invalidated[0]

  return (
    <div className="alert alert--invalidated" role="alert" data-testid="invalidated-approval-warning">
      <div className="alert__head">
        <span className="alert__icon" aria-hidden="true">
          ⚠
        </span>
        <h3 className="alert__title">
          No valid approval covers the current commit — the previous approval was invalidated
        </h3>
      </div>
      <p className="alert__body">
        This job has {invalidated.length} invalidated approval{invalidated.length === 1 ? '' : 's'} and{' '}
        <strong>no authorization for commit </strong>
        <code>{job.head_sha.slice(0, 12)}</code>. The pull request moved after the decision was
        recorded, so the earlier authorization no longer applies. Re-approval is required before this
        change is authorized.
      </p>
      {latest !== undefined ? (
        <ul className="alert__facts">
          <li>
            <span className="muted">Decision: </span>
            {approvalDecisionMeta(latest.decision).label} by {latest.decided_by}{' '}
            <RelativeTime value={latest.decided_at} />
          </li>
          <li>
            <span className="muted">Covered commit: </span>
            <code>{latest.commit_sha.slice(0, 12)}</code>
          </li>
          <li>
            <span className="muted">Current head commit: </span>
            <code>{job.head_sha.slice(0, 12)}</code>
          </li>
          <li>
            <span className="muted">Invalidated: </span>
            <RelativeTime value={latest.invalidated_at} fallback="not recorded" />
            {latest.invalidated_reason !== null && latest.invalidated_reason !== ''
              ? ` — ${latest.invalidated_reason}`
              : ''}
          </li>
        </ul>
      ) : null}
      <p className="alert__action">
        Use the human authorization control above to record a decision for the current commit.
      </p>
    </div>
  )
}
