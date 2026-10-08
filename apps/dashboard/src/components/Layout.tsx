import type { ReactNode } from 'react'

export interface PageHeaderProps {
  title: string
  /** One-line statement of what the page shows. */
  description?: ReactNode
  /** Buttons or controls rendered on the right. */
  actions?: ReactNode
}

/** Page-level heading block. The shell owns the `<h1>`; this owns the rest. */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <div className="page-header">
      <div className="page-header__text">
        <h2 className="page-header__title">{title}</h2>
        {description !== undefined ? (
          <p className="page-header__description muted">{description}</p>
        ) : null}
      </div>
      {actions !== undefined ? <div className="page-header__actions">{actions}</div> : null}
    </div>
  )
}

export interface PanelProps {
  title?: string
  description?: ReactNode
  actions?: ReactNode
  children: ReactNode
  /** Remove inner padding for tables that handle their own spacing. */
  flush?: boolean
  className?: string
  /** Set when the panel is a tabpanel target so `aria-labelledby` can point at the title. */
  id?: string
}

/** A titled content region. Every page composes its layout from Panels. */
export function Panel({
  title,
  description,
  actions,
  children,
  flush = false,
  className,
  id,
}: PanelProps) {
  const classes = ['panel']
  if (flush) classes.push('panel--flush')
  if (className !== undefined) classes.push(className)
  return (
    <section className={classes.join(' ')} id={id}>
      {title !== undefined || actions !== undefined ? (
        <header className="panel__header">
          <div>
            {title !== undefined ? <h3 className="panel__title">{title}</h3> : null}
            {description !== undefined ? (
              <p className="panel__description muted">{description}</p>
            ) : null}
          </div>
          {actions !== undefined ? <div className="panel__actions">{actions}</div> : null}
        </header>
      ) : null}
      <div className="panel__body">{children}</div>
    </section>
  )
}

export interface StatCardProps {
  label: string
  value: ReactNode
  /** Secondary line: a denominator, a comparison, or an explanation. */
  hint?: ReactNode
  tone?: 'default' | 'good' | 'warn' | 'bad'
}

/** A single metric. Used by the dashboard and the agent/tool rosters. */
export function StatCard({ label, value, hint, tone = 'default' }: StatCardProps) {
  return (
    <div className={`stat-card stat-card--${tone}`}>
      <span className="stat-card__label">{label}</span>
      <span className="stat-card__value">{value}</span>
      {hint !== undefined ? <span className="stat-card__hint muted">{hint}</span> : null}
    </div>
  )
}
