/**
 * Offset/limit pagination helpers.
 *
 * The API paginates with `limit`/`offset` and returns `total`. The UI keeps
 * `page` in the URL because an operator pastes links to each other, so these
 * functions are the single place the two representations convert.
 */

export const DEFAULT_PAGE_SIZE = 25
export const PAGE_SIZE_OPTIONS: readonly number[] = [10, 25, 50, 100]

export interface PaginationState {
  page: number
  pageSize: number
  offset: number
}

/** Clamp a 1-based page number to a sane integer. */
export function normalizePage(page: number | null | undefined): number {
  if (page === null || page === undefined || !Number.isFinite(page)) return 1
  const asInt = Math.floor(page)
  return asInt < 1 ? 1 : asInt
}

export function offsetFor(page: number, pageSize: number): number {
  return (normalizePage(page) - 1) * pageSize
}

export function pageFor(offset: number, pageSize: number): number {
  if (!Number.isFinite(offset) || offset <= 0) return 1
  return Math.floor(offset / pageSize) + 1
}

export function pageCount(total: number, pageSize: number): number {
  if (!Number.isFinite(total) || total <= 0 || pageSize <= 0) return 1
  return Math.max(1, Math.ceil(total / pageSize))
}

/** True when there is a next page after `page`. */
export function hasNextPage(page: number, total: number, pageSize: number): boolean {
  return page < pageCount(total, pageSize)
}

export function hasPreviousPage(page: number): boolean {
  return normalizePage(page) > 1
}
