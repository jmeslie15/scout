import type { JobStage } from '../api/types'
import { formatDuration, humanizeEnum, stageOutcomeMeta } from '../lib/badges'
import { Badge } from './Badge'
import { RelativeTime } from './RelativeTime'

export interface StageTimelineProps {
  stages: readonly JobStage[]
  /** The job's current status, used to explain what is running right now. */
  currentStatus?: string
}

/**
 * The canonical pipeline order from `docs/ARCHITECTURE.md` section 4.1. The
 * orchestrator derives the job status from the first unresolved stage, so
 * rendering the pipeline in this fixed order — including stages the job has not
 * reached yet — is what lets an operator see progress at a glance.
 */
const PIPELINE_ORDER: readonly string[] = [
  'PREPARING',
  'ANALYZING',
  'REVIEWING',
  'SECURITY_SCANNING',
  'TESTING',
  'FEATURE_VERIFYING',
  'INVESTIGATING',
]

/**
 * Stage pipeline for one job.
 *
 * A stage row is a first-class record: `SKIPPED` always carries a `skip_reason`
 * (the domain model refuses a skip without one) and both `FAILED` and
 * `CANCELLED` rows show their `error`. Nothing is silently skipped, and this
 * component is where that property is visible.
 *
 * `stages` is append-only across attempts, so the newest attempt for each stage
 * is the one rendered, with a note when earlier attempts exist.
 */
export function StageTimeline({ stages, currentStatus }: StageTimelineProps) {
  // Latest attempt wins. Stages the API reports that are not in the fixed
  // pipeline are appended in the order given, not dropped.
  const latestByStage = new Map<string, JobStage>()
  const attemptCounts = new Map<string, number>()
  for (const stage of stages) {
    attemptCounts.set(stage.stage, (attemptCounts.get(stage.stage) ?? 0) + 1)
    const existing = latestByStage.get(stage.stage)
    if (
      existing === undefined ||
      stage.attempt > existing.attempt ||
      (stage.attempt === existing.attempt &&
        (stage.started_at ?? '') > (existing.started_at ?? ''))
    ) {
      latestByStage.set(stage.stage, stage)
    }
  }

  const extraStages = [...latestByStage.keys()].filter((stage) => !PIPELINE_ORDER.includes(stage))
  const orderedNames = [...PIPELINE_ORDER, ...extraStages]

  if (stages.length === 0) {
    return (
      <div className="stage-timeline stage-timeline--empty">
        <p className="muted">
          No stage has been recorded yet.
          {currentStatus !== undefined && currentStatus !== 'RECEIVED' && currentStatus !== 'QUEUED'
            ? ` The job is in ${humanizeEnum(currentStatus)}.`
            : ''}
        </p>
      </div>
    )
  }

  return (
    <ol className="stage-timeline" aria-label="Job stage pipeline">
      {orderedNames.map((name) => {
        const stage = latestByStage.get(name)
        const attempts = attemptCounts.get(name) ?? 0
        if (stage === undefined) {
          return (
            <li key={name} className="stage-timeline__item stage-timeline__item--not-reached">
              <span className="stage-timeline__marker" aria-hidden="true" />
              <div className="stage-timeline__body">
                <div className="stage-timeline__head">
                  <span className="stage-timeline__name">{humanizeEnum(name)}</span>
                  <Badge tone="neutral" description="This stage has not been entered yet.">
                    NOT REACHED
                  </Badge>
                </div>
              </div>
            </li>
          )
        }

        const meta = stageOutcomeMeta(stage.outcome)
        const isRunning = stage.outcome === 'RUNNING'
        return (
          <li
            key={name}
            className={`stage-timeline__item stage-timeline__item--${meta.tone}`}
          >
            <span className="stage-timeline__marker" aria-hidden="true" />
            <div className="stage-timeline__body">
              <div className="stage-timeline__head">
                <span className="stage-timeline__name">{humanizeEnum(name)}</span>
                <Badge tone={meta.tone} description={meta.description} pulse={isRunning}>
                  {meta.label}
                </Badge>
                {attempts > 0 ? (
                  <span className="stage-timeline__attempt muted">
                    attempt {stage.attempt}
                    {attempts > 1 ? ` of ${attempts} recorded` : ''}
                  </span>
                ) : null}
                <span className="stage-timeline__duration muted">
                  {formatDuration(stage.duration_seconds)}
                </span>
              </div>
              <div className="stage-timeline__times muted">
                {stage.started_at !== null ? (
                  <>
                    started <RelativeTime value={stage.started_at} />
                  </>
                ) : (
                  'not started'
                )}
                {stage.completed_at !== null ? (
                  <>
                    {' · '}finished <RelativeTime value={stage.completed_at} />
                  </>
                ) : null}
              </div>
              {stage.skip_reason !== null && stage.skip_reason !== '' ? (
                <p className="stage-timeline__note stage-timeline__note--skip">
                  <strong>Skipped:</strong> {stage.skip_reason}
                </p>
              ) : null}
              {stage.error !== null && stage.error !== '' ? (
                <p className="stage-timeline__note stage-timeline__note--error">
                  <strong>Error:</strong> {stage.error}
                </p>
              ) : null}
            </div>
          </li>
        )
      })}
    </ol>
  )
}
