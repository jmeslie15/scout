/**
 * Table primitives.
 *
 * Deliberately thin wrappers over semantic HTML rather than a generic
 * `<DataTable columns={...}>` component: the policies are "avoid framework
 * magic" and "do not introduce infrastructure before it is necessary", and a
 * column-config abstraction buys nothing here while making the markup harder to
 * read and the types harder to get right.
 *
 * What the wrappers do buy is consistency: a scroll container for narrow
 * viewports, `aria-sort` handling, and one place to change density.
 */

import type { ReactNode, ThHTMLAttributes } from 'react'

export interface TableProps {
  /** Accessible description of the table's contents. */
  caption: string
  /** Hide the caption visually but keep it for assistive tech. Default true. */
  visuallyHiddenCaption?: boolean
  children: ReactNode
}

export function Table({ caption, visuallyHiddenCaption = true, children }: TableProps) {
  return (
    <div className="table-scroll">
      <table className="table">
        <caption className={visuallyHiddenCaption ? 'visually-hidden' : 'table__caption'}>
          {caption}
        </caption>
        {children}
      </table>
    </div>
  )
}

export function THead({ children }: { children: ReactNode }) {
  return <thead>{children}</thead>
}

export function TBody({ children }: { children: ReactNode }) {
  return <tbody>{children}</tbody>
}

export interface TRProps {
  children: ReactNode
  /** Marks the row as expanded/selected for styling only. */
  selected?: boolean
  className?: string
}

export function TR({ children, selected = false, className }: TRProps) {
  const classes = ['table__row']
  if (selected) classes.push('table__row--selected')
  if (className !== undefined) classes.push(className)
  return <tr className={classes.join(' ')}>{children}</tr>
}

export type SortDirection = 'ascending' | 'descending'

export interface THProps extends ThHTMLAttributes<HTMLTableCellElement> {
  children: ReactNode
  /** Current sort direction for this column, if it is the sorted column. */
  sort?: SortDirection | undefined
  /** Right-align the header to match a numeric column. */
  numeric?: boolean
}

export function TH({ children, sort, numeric = false, ...rest }: THProps) {
  const className = [rest.className, numeric ? 'table__head--numeric' : undefined]
    .filter((value): value is string => value !== undefined && value !== '')
    .join(' ')
  return (
    <th scope="col" aria-sort={sort} {...rest} className={className}>
      {children}
    </th>
  )
}

export interface TDProps {
  children: ReactNode
  /** Right-align numeric columns. */
  numeric?: boolean
  colSpan?: number
  className?: string
}

export function TD({ children, numeric = false, colSpan, className }: TDProps) {
  const classes = ['table__cell']
  if (numeric) classes.push('table__cell--numeric')
  if (className !== undefined) classes.push(className)
  return (
    <td className={classes.join(' ')} colSpan={colSpan}>
      {children}
    </td>
  )
}
