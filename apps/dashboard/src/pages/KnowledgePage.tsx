import { useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useAsync } from '../api/hooks'
import type { ItemsResponse, KnowledgeDocument, KnowledgeDocumentRequest } from '../api/types'
import { AsyncBoundary } from '../components/AsyncBoundary'
import { Badge } from '../components/Badge'
import { EmptyState } from '../components/EmptyState'
import { PageHeader, Panel } from '../components/Layout'
import { RelativeTime } from '../components/RelativeTime'
import { useQueryState } from '../lib/useQueryState'

/**
 * Scout's engineering memory.
 *
 * Sources are ranked by authority, and only humans and approved decisions are
 * AUTHORITATIVE. A knowledge document is evidence at AUTHORITATIVE strength for
 * `human_answer` / `knowledge_document` kinds, which is why the authority field
 * is displayed prominently rather than tucked into a details panel.
 */
const AUTHORITY_TONES: Record<string, 'human' | 'verified' | 'ai' | 'neutral'> = {
  HUMAN: 'human',
  APPROVED_DECISION: 'human',
  REPOSITORY: 'verified',
  TOOL: 'verified',
  AGENT: 'ai',
  INFERRED: 'ai',
}

const SOURCE_TYPES = [
  'HUMAN_ANSWER',
  'ENGINEERING_DECISION',
  'RUNBOOK',
  'ADR',
  'REPOSITORY_DOC',
  'INCIDENT',
  'CONVENTION',
] as const

const AUTHORITIES = ['HUMAN', 'APPROVED_DECISION', 'REPOSITORY', 'TOOL', 'AGENT', 'INFERRED'] as const

export function KnowledgePage() {
  const query = useQueryState()
  const scope = query.get('scope', '') ?? ''
  const search = query.get('query', '') ?? ''

  const [searchDraft, setSearchDraft] = useState(search)

  const documents = useAsync<ItemsResponse<KnowledgeDocument>>(
    (signal) => api.listKnowledge({ scope, query: search, limit: 100 }, { signal }),
    [scope, search],
  )

  const [authority, setAuthority] = useState<string>('')
  const [showForm, setShowForm] = useState(false)

  const items = useMemo(() => {
    const all = documents.data?.items ?? []
    const filtered = authority === '' ? all : all.filter((item) => item.authority === authority)
    return [...filtered].sort((a, b) => (b.updated_at ?? '').localeCompare(a.updated_at ?? ''))
  }, [documents.data, authority])

  return (
    <>
      <PageHeader
        title="Knowledge"
        description="Engineering memory: answers and decisions Scout can cite. A human answer or an approved decision outranks anything Scout inferred."
        actions={
          <>
            <button type="button" className="button" onClick={documents.refetch}>
              Refresh
            </button>{' '}
            <button
              type="button"
              className="button button--primary"
              aria-expanded={showForm}
              onClick={() => setShowForm((value) => !value)}
            >
              {showForm ? 'Close form' : 'Add document'}
            </button>
          </>
        }
      />

      {showForm ? (
        <KnowledgeForm
          onCreated={() => {
            documents.refetch()
            setShowForm(false)
          }}
        />
      ) : null}

      <Panel title="Filter">
        <form
          className="filter-bar"
          onSubmit={(event) => {
            event.preventDefault()
            query.update({ query: searchDraft, scope, page: null })
          }}
        >
          <label className="field">
            <span className="field__label">Keyword search</span>
            <input
              type="search"
              className="input"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              placeholder="title or body"
            />
          </label>
          <label className="field">
            <span className="field__label">Scope</span>
            <input
              type="text"
              className="input"
              value={scope}
              onChange={(event) => query.update({ scope: event.target.value, page: null })}
              placeholder="repository:edrevel/scout"
            />
          </label>
          <label className="field">
            <span className="field__label">Authority</span>
            <select
              className="select"
              value={authority}
              onChange={(event) => setAuthority(event.target.value)}
            >
              <option value="">All authorities</option>
              {AUTHORITIES.map((value) => (
                <option key={value} value={value}>
                  {value.replace(/_/g, ' ')}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" className="button">
            Search
          </button>
          {search !== '' || scope !== '' || authority !== '' ? (
            <button
              type="button"
              className="button button--ghost"
              onClick={() => {
                setSearchDraft('')
                setAuthority('')
                query.clear(['query', 'scope'])
              }}
            >
              Clear
            </button>
          ) : null}
        </form>
        <p className="muted">
          Authority is filtered in the browser because the endpoint's only documented parameters are
          `scope`, `query`, and `limit`.
        </p>
      </Panel>

      <AsyncBoundary state={documents} context="knowledge documents" errorOverridesData>
        {documents.data !== null ? (
          items.length === 0 ? (
            <Panel>
              <EmptyState
                title="No documents match this view"
                description={
                  search !== '' || scope !== ''
                    ? 'Try a broader keyword or clear the scope filter.'
                    : 'Scout has no engineering memory recorded yet. Add a document, or promote an answer from the Questions page.'
                }
              />
            </Panel>
          ) : (
            <div className="stack">
              {items.map((document) => (
                <article key={document.id} className="knowledge-card">
                  <header className="knowledge-card__head">
                    <h3 className="knowledge-card__title">{document.title}</h3>
                    <Badge
                      tone={AUTHORITY_TONES[document.authority] ?? 'neutral'}
                      description={`Authority ${document.authority}. AUTHORITATIVE strength outranks anything Scout inferred.`}
                    >
                      {document.authority.replace(/_/g, ' ')}
                    </Badge>
                    <code className="knowledge-card__scope">{document.scope}</code>
                    <span className="muted knowledge-card__meta">
                      {document.source_type.replace(/_/g, ' ')} · updated{' '}
                      <RelativeTime value={document.updated_at} />
                    </span>
                  </header>
                  <p className="knowledge-card__body">{document.body}</p>
                  <footer className="knowledge-card__footer">
                    {document.tags.length > 0 ? (
                      <ul className="tag-list" aria-label="Tags">
                        {document.tags.map((tag) => (
                          <li key={tag} className="tag">
                            {tag}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <span className="muted">No tags</span>
                    )}
                    {document.superseded_by_id !== null ? (
                      <span
                        className="marker marker--invalidated"
                        title={`Superseded by document ${document.superseded_by_id}`}
                      >
                        Superseded — do not cite
                      </span>
                    ) : null}
                  </footer>
                </article>
              ))}
            </div>
          )
        ) : null}
      </AsyncBoundary>
    </>
  )
}

function KnowledgeForm({ onCreated }: { onCreated: () => void }) {
  const [draft, setDraft] = useState<KnowledgeDocumentRequest>({
    title: '',
    body: '',
    source_type: SOURCE_TYPES[0],
    authority: AUTHORITIES[0],
    scope: '',
    tags: [],
  })
  const [tagsText, setTagsText] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<ApiError | Error | null>(null)

  const submit = async (): Promise<void> => {
    setError(null)
    if (draft.title.trim() === '' || draft.body.trim() === '' || draft.scope.trim() === '') {
      setError(new Error('Title, body, and scope are all required.'))
      return
    }
    const tags = tagsText
      .split(',')
      .map((tag) => tag.trim())
      .filter((tag) => tag !== '')

    setPending(true)
    try {
      await api.createKnowledge({ ...draft, tags })
      setDraft({ ...draft, title: '', body: '', tags: [] })
      setTagsText('')
      onCreated()
    } catch (caught) {
      setError(caught instanceof Error ? caught : new Error(String(caught)))
    } finally {
      setPending(false)
    }
  }

  return (
    <Panel title="Add a knowledge document" description="Written to Scout's engineering memory.">
      <div className="form-grid">
        <label className="field" htmlFor="knowledge-title">
          <span className="field__label">
            Title <span className="required-marker">(required)</span>
          </span>
          <input
            id="knowledge-title"
            className="input"
            type="text"
            value={draft.title}
            required
            onChange={(event) => setDraft({ ...draft, title: event.target.value })}
          />
        </label>
        <label className="field" htmlFor="knowledge-scope">
          <span className="field__label">
            Scope <span className="required-marker">(required)</span>
          </span>
          <input
            id="knowledge-scope"
            className="input"
            type="text"
            value={draft.scope}
            required
            placeholder="repository:edrevel/scout"
            onChange={(event) => setDraft({ ...draft, scope: event.target.value })}
          />
        </label>
        <label className="field" htmlFor="knowledge-source-type">
          <span className="field__label">Source type</span>
          <select
            id="knowledge-source-type"
            className="select"
            value={draft.source_type}
            onChange={(event) => setDraft({ ...draft, source_type: event.target.value })}
          >
            {SOURCE_TYPES.map((value) => (
              <option key={value} value={value}>
                {value.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
        </label>
        <label className="field" htmlFor="knowledge-authority">
          <span className="field__label">Authority</span>
          <select
            id="knowledge-authority"
            className="select"
            value={draft.authority}
            onChange={(event) => setDraft({ ...draft, authority: event.target.value })}
          >
            {AUTHORITIES.map((value) => (
              <option key={value} value={value}>
                {value.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
        </label>
        <label className="field field--wide" htmlFor="knowledge-tags">
          <span className="field__label">Tags</span>
          <input
            id="knowledge-tags"
            className="input"
            type="text"
            value={tagsText}
            placeholder="comma, separated, tags"
            onChange={(event) => setTagsText(event.target.value)}
          />
        </label>
        <label className="field field--wide" htmlFor="knowledge-body">
          <span className="field__label">
            Body <span className="required-marker">(required)</span>
          </span>
          <textarea
            id="knowledge-body"
            className="textarea"
            rows={6}
            value={draft.body}
            required
            onChange={(event) => setDraft({ ...draft, body: event.target.value })}
          />
        </label>
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
          {pending ? 'Saving…' : 'Save document'}
        </button>
      </div>
      <div aria-live="polite">
        {error !== null ? (
          <p className="form-error" role="alert">
            {error instanceof ApiError ? error.displayMessage : error.message}
          </p>
        ) : null}
      </div>
    </Panel>
  )
}
