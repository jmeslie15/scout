import { PAGE_SIZE_OPTIONS } from '../lib/pagination'

export interface PaginationProps {
  page: number
  pageSize: number
  total: number
  /** True while the next/previous page is loading. */
  busy?: boolean
  onPageChange: (page: number) => void
  onPageSizeChange?: (pageSize: number) => void
}

/**
 * Offset/limit pagination control.
 *
 * Buttons are real `<button>` elements and are disabled (not hidden) at the
 * boundaries, so keyboard focus does not jump when a page fills up. The
 * "showing X–Y of Z" text is `aria-live` so a screen-reader user learns that the
 * page changed.
 */
export function Pagination({
  page,
  pageSize,
  total,
  busy = false,
  onPageChange,
  onPageSizeChange,
}: PaginationProps) {
  const firstItem = total === 0 ? 0 : (page - 1) * pageSize + 1
  const lastItem = Math.min(page * pageSize, total)
  const lastPage = Math.max(1, Math.ceil(total / pageSize))

  return (
    <div className="pagination">
      <p className="pagination__summary muted" aria-live="polite">
        Showing {firstItem}–{lastItem} of {total}
      </p>
      <div className="pagination__controls">
        {onPageSizeChange !== undefined ? (
          <label className="field field--inline">
            <span className="field__label">Rows</span>
            <select
              className="select"
              value={pageSize}
              onChange={(event) => onPageSizeChange(Number(event.target.value))}
            >
              {PAGE_SIZE_OPTIONS.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <button
          type="button"
          className="button button--small"
          onClick={() => onPageChange(page - 1)}
          disabled={busy || page <= 1}
        >
          Previous
        </button>
        <span className="pagination__page">
          Page {page} of {lastPage}
        </span>
        <button
          type="button"
          className="button button--small"
          onClick={() => onPageChange(page + 1)}
          disabled={busy || page >= lastPage}
        >
          Next
        </button>
      </div>
    </div>
  )
}
