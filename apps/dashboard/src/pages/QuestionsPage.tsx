import { useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import { useAsync } from '../api/hooks'
import { ALL_QUESTION_STATUSES } from '../api/types'
import type {
  AnswerQuestionRequest,
  ItemsResponse,
  Paginated,
  Question,
  RepositorySummary,
} from '../api/types'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { Badge } from '../components/Badge'
import { EmptyState } from '../components/EmptyState'
import { EvidenceList } from '../components/EvidenceList'
import { PageHeader, Panel } from '../components/Layout'
import { Pagination } from '../components/Pagination'
import { RelativeTime } from '../components/RelativeTime'
import { SeverityBadge } from '../components/SeverityBadge'
import { DEFAULT_PAGE_SIZE, normalizePage } from '../lib/pagination'
import { useQueryState } from '../lib/useQueryState'

const DEFAULT_QUESTION_STATUS = 'OPEN'

/**
 * Questions: Scout's human escalations.
 *
 * Each question is expandable and shows everything the domain model carries —
 * description, why Scout is asking (`investigation_summary`), the options, the
 * default assumption Scout would otherwise make, the context it had, its
 * evidence, and any answers already given. Answering posts to the answer
 * endpoint and refetches the list, so an answered question immediately moves out
 * of the OPEN filter rather than appearing to be ignored.
 */
export function QuestionsPage() {
  const query = useQueryState()
  const status = query.get('status', DEFAULT_QUESTION_STATUS) ?? ''
  const repository = query.get('repository', '') ?? ''
  const page = normalizePage(query.getNumber('page', 1))
  const limit = DEFAULT_PAGE_SIZE
  const offset = (page - 1) * limit

  const questions = useAsync<Paginated<Question>>(
    (signal) => api.listQuestions({ status, repository, limit, offset }, { signal }),
    [status, repository, limit, offset],
  )

  const repositories = useAsync<ItemsResponse<RepositorySummary>>(
    (signal) => api.listRepositories({ signal }),
    [],
  )

  const [expandedId, setExpandedId] = useState<string | null>(null)

  return (
    <>
      <PageHeader
        title="Questions"
        description="Scout escalates rather than guessing. A question with no answer blocks a PASS recommendation, and its answer can be promoted into Scout's engineering memory."
        actions={
          <button type="button" className="button" onClick={questions.refetch}>
            Refresh
          </button>
        }
      />

      <Panel title="Filters">
        <div className="filter-bar">
          <label className="field">
            <span className="field__label">Status</span>
            <select
              className="select"
              value={status}
              onChange={(event) => query.update({ status: event.target.value, page: 1 })}
            >
              <option value="">All statuses</option>
              {ALL_QUESTION_STATUSES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field__label">Repository</span>
            <select
              className="select"
              value={repository}
              onChange={(event) => query.update({ repository: event.target.value, page: 1 })}
            >
              <option value="">All repositories</option>
              {(repositories.data?.items ?? []).map((item) => (
                <option key={item.full_name} value={item.full_name}>
                  {item.full_name}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="button button--ghost"
            onClick={() => query.clear(['status', 'repository', 'page'])}
          >
            Show open questions
          </button>
        </div>
      </Panel>

      <AsyncBoundary state={questions} context="questions" errorOverridesData>
        {questions.data !== null ? (
          questions.data.items.length === 0 ? (
            <Panel>
              <EmptyState
                title="No questions match this view"
                description="Scout has nothing outstanding here. Switch the status filter to see answered or dismissed questions."
              />
            </Panel>
          ) : (
            <div className="stack">
              {questions.data.items.map((question) => (
                <QuestionCard
                  key={question.id}
                  question={question}
                  expanded={expandedId === question.id}
                  onToggle={() => setExpandedId(expandedId === question.id ? null : question.id)}
                  onAnswered={() => {
                    questions.refetch()
                  }}
                />
              ))}
            </div>
          )
        ) : null}
      </AsyncBoundary>

      {questions.data !== null ? (
        <Pagination
          page={page}
          pageSize={limit}
          total={questions.data.total}
          busy={questions.refreshing}
          onPageChange={(next) => query.update({ page: next < 1 ? 1 : next })}
        />
      ) : null}
    </>
  )
}

export interface QuestionCardProps {
  question: Question
  expanded: boolean
  onToggle: () => void
  onAnswered: () => void
}

export function QuestionCard({ question, expanded, onToggle, onAnswered }: QuestionCardProps) {
  const bodyId = useId()
  return (
    <article className="question-card">
      <div className="question-card__head">
        <SeverityBadge severity={question.severity} />
        <h3 className="question-card__title">{question.title}</h3>
        <Badge
          tone={
            question.status === 'OPEN' ? 'human' : question.status === 'ANSWERED' ? 'verified' : 'neutral'
          }
          description={`Question status ${question.status}.`}
        >
          {question.status}
        </Badge>
        <span className="muted question-card__meta">
          <RelativeTime value={question.created_at} /> · asked by {question.created_by}
        </span>
        <button
          type="button"
          className="button button--ghost button--small"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={onToggle}
        >
          {expanded ? 'Collapse' : 'Expand'}
          <span className="visually-hidden"> question {question.title}</span>
        </button>
      </div>

      {question.answers.length > 0 ? (
        <div className="question-card__answered">
          <strong>Answered by {question.answers[question.answers.length - 1]?.answered_by}:</strong>{' '}
          {question.answers[question.answers.length - 1]?.body}
        </div>
      ) : null}

      <div id={bodyId} hidden={!expanded} className="question-card__body">
        <section className="question-card__section">
          <h4>What Scout found</h4>
          <p>{question.description}</p>
        </section>

        <section className="question-card__section">
          <h4>Why Scout is asking</h4>
          <p>{question.investigation_summary}</p>
        </section>

        <section className="question-card__section">
          <h4>Context</h4>
          <p>{question.context}</p>
        </section>

        {question.options.length > 0 ? (
          <section className="question-card__section">
            <h4>Options Scout considered</h4>
            <ul className="question-card__options">
              {question.options.map((option) => (
                <li key={option}>{option}</li>
              ))}
            </ul>
          </section>
        ) : null}

        {question.default_assumption !== null && question.default_assumption !== '' ? (
          <section className="question-card__section">
            <h4>Default assumption</h4>
            <p className="question-card__assumption">
              If no one answers, Scout will proceed on: <em>{question.default_assumption}</em>
            </p>
          </section>
        ) : null}

        <EvidenceList evidence={question.evidence} />

        <section className="question-card__section">
          <h4>Previous answers</h4>
          {question.answers.length === 0 ? (
            <p className="muted">No answer has been recorded yet.</p>
          ) : (
            <ul className="answer-list">
              {question.answers.map((answer) => (
                <li key={answer.id} className="answer-list__item">
                  <p className="answer-list__body">{answer.body}</p>
                  <p className="muted answer-list__meta">
                    {answer.answered_by} · <RelativeTime value={answer.answered_at} />
                  </p>
                </li>
              ))}
            </ul>
          )}
        </section>

        {question.status === 'OPEN' ? (
          <AnswerForm question={question} onAnswered={onAnswered} />
        ) : (
          <p className="muted">
            This question is {question.status.toLowerCase()}, so the answer form is closed.
          </p>
        )}

        <p className="muted">
          <Link to={`/jobs/${question.job_id}`}>Open the job this question came from</Link>
        </p>
      </div>
    </article>
  )
}

interface AnswerFormProps {
  question: Question
  onAnswered: () => void
}

/**
 * Inline answer form.
 *
 * `answered_by` is required for the same reason as an approval's `decided_by`:
 * an unattributed answer would enter Scout's knowledge base with no accountable
 * author. Promoting to knowledge is opt-in and reveals a scope field, because a
 * promoted answer becomes AUTHORITATIVE evidence for future jobs.
 */
export function AnswerForm({ question, onAnswered }: AnswerFormProps) {
  const [body, setBody] = useState('')
  const [answeredBy, setAnsweredBy] = useState('')
  const [promote, setPromote] = useState(false)
  const [scope, setScope] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<ApiError | Error | null>(null)
  const [success, setSuccess] = useState(false)

  const fieldPrefix = useId()

  const submit = async (): Promise<void> => {
    setError(null)
    setSuccess(false)

    if (body.trim() === '') {
      setError(new Error('An answer body is required.'))
      return
    }
    if (answeredBy.trim() === '') {
      setError(new Error('Enter who is answering — the answer is attributed in the audit log.'))
      return
    }
    if (promote && scope.trim() === '') {
      setError(
        new Error(
          'A knowledge scope is required when promoting to knowledge, so the answer is not applied outside the area it was written for.',
        ),
      )
      return
    }

    const payload: AnswerQuestionRequest = {
      body: body.trim(),
      answered_by: answeredBy.trim(),
      promote_to_knowledge: promote,
      knowledge_scope: promote ? scope.trim() : null,
    }

    setPending(true)
    try {
      await api.answerQuestion(question.id, payload)
      setBody('')
      setSuccess(true)
      onAnswered()
    } catch (caught) {
      setError(caught instanceof Error ? caught : new Error(String(caught)))
    } finally {
      setPending(false)
    }
  }

  return (
    <section className="answer-form" aria-labelledby={`${fieldPrefix}-heading`}>
      <h4 id={`${fieldPrefix}-heading`}>Answer this question</h4>
      <div className="answer-form__fields">
        <label className="field" htmlFor={`${fieldPrefix}-body`}>
          <span className="field__label">
            Answer <span className="required-marker">(required)</span>
          </span>
          <textarea
            id={`${fieldPrefix}-body`}
            className="textarea"
            rows={4}
            value={body}
            required
            onChange={(event) => setBody(event.target.value)}
            placeholder="The engineering answer, with enough context to be useful later."
          />
        </label>

        <label className="field" htmlFor={`${fieldPrefix}-by`}>
          <span className="field__label">
            Answered by <span className="required-marker">(required)</span>
          </span>
          <input
            id={`${fieldPrefix}-by`}
            className="input"
            type="text"
            value={answeredBy}
            required
            onChange={(event) => setAnsweredBy(event.target.value)}
            placeholder="you@edrevel.ai"
          />
        </label>

        <div className="answer-form__promote">
          <label className="checkbox" htmlFor={`${fieldPrefix}-promote`}>
            <input
              id={`${fieldPrefix}-promote`}
              type="checkbox"
              checked={promote}
              onChange={(event) => setPromote(event.target.checked)}
            />
            <span>Promote this answer to Scout's knowledge base</span>
          </label>
          <p className="field__hint">
            A promoted answer becomes AUTHORITATIVE evidence for future jobs, so binding it to a scope
            keeps it from being applied where it does not hold.
          </p>
          {promote ? (
            <label className="field" htmlFor={`${fieldPrefix}-scope`}>
              <span className="field__label">
                Knowledge scope <span className="required-marker">(required)</span>
              </span>
              <input
                id={`${fieldPrefix}-scope`}
                className="input"
                type="text"
                value={scope}
                required
                onChange={(event) => setScope(event.target.value)}
                placeholder="repository:edrevel/scout or service:billing"
              />
            </label>
          ) : null}
        </div>
      </div>

      <div className="button-row">
        <button
          type="button"
          className="button button--primary"
          disabled={pending}
          onClick={() => {
            void submit()
          }}
        >
          {pending ? 'Submitting…' : 'Submit answer'}
        </button>
      </div>

      <div aria-live="polite">
        {error !== null ? (
          <p className="form-error" role="alert">
            {error instanceof ApiError ? error.displayMessage : error.message}
          </p>
        ) : null}
        {success ? <p className="form-success">Answer recorded. The list has been refreshed.</p> : null}
      </div>
    </section>
  )
}
