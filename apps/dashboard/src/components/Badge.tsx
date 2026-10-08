import type { ReactNode } from 'react'
import type { BadgeTone } from '../lib/badges'

export interface BadgeProps {
  tone: BadgeTone
  /** Optional leading glyph, e.g. a severity dot. */
  glyph?: ReactNode
  /** Explained in a `title` so colour is never the only carrier of meaning. */
  description?: string
  /** Renders a pulsing dot, used for in-flight job statuses. */
  pulse?: boolean
  children: ReactNode
  className?: string
}

/**
 * The one badge primitive. Every domain badge (status, severity, risk,
 * provenance, evidence strength) is a thin total-mapping wrapper around it, so
 * the palette is defined in exactly one place: `src/styles.css`.
 *
 * A badge always carries its text label as well as its colour, because colour
 * alone is not an accessible signal.
 */
export function Badge({
  tone,
  glyph,
  description,
  pulse = false,
  children,
  className,
}: BadgeProps) {
  const classes = ['badge', `badge--${tone}`]
  if (className !== undefined) classes.push(className)
  return (
    <span className={classes.join(' ')} title={description} data-tone={tone}>
      {pulse ? <span className="badge__pulse" aria-hidden="true" /> : null}
      {glyph !== undefined ? (
        <span className="badge__glyph" aria-hidden="true">
          {glyph}
        </span>
      ) : null}
      <span className="badge__label">{children}</span>
    </span>
  )
}
