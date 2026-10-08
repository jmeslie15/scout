import { useId, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'

export interface TabDefinition {
  id: string
  label: string
  /** Optional count badge, e.g. the number of findings. */
  count?: number
  /** Rendered when this tab is selected. */
  content: () => ReactNode
  /** Dim the tab label, e.g. for a section with nothing recorded. */
  muted?: boolean
}

export interface TabsProps {
  tabs: readonly TabDefinition[]
  /** Label for the tab list, announced to assistive technology. */
  label: string
  initialTabId?: string
}

/**
 * A sectioned view for the job detail page.
 *
 * Implemented with the ARIA tabs pattern: roving `tabIndex` and Left/Right/Home/
 * End arrow handling, so the whole tab strip is one keyboard stop rather than
 * ten. Tab state is deliberately local rather than in the URL — it is a reading
 * preference, not a shareable view, and the ten sections are not independent
 * resources.
 */
export function Tabs({ tabs, label, initialTabId }: TabsProps) {
  const baseId = useId()
  const firstTab = tabs[0]
  const [selected, setSelected] = useState<string>(
    initialTabId ?? firstTab?.id ?? '',
  )

  if (tabs.length === 0) return null

  const selectedIndex = Math.max(
    0,
    tabs.findIndex((tab) => tab.id === selected),
  )
  const activeTab = tabs[selectedIndex]

  const focusTab = (index: number): void => {
    const bounded = (index + tabs.length) % tabs.length
    const target = tabs[bounded]
    if (target === undefined) return
    setSelected(target.id)
    const element = document.getElementById(`${baseId}-tab-${target.id}`)
    element?.focus()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    switch (event.key) {
      case 'ArrowRight':
        event.preventDefault()
        focusTab(selectedIndex + 1)
        break
      case 'ArrowLeft':
        event.preventDefault()
        focusTab(selectedIndex - 1)
        break
      case 'Home':
        event.preventDefault()
        focusTab(0)
        break
      case 'End':
        event.preventDefault()
        focusTab(tabs.length - 1)
        break
      default:
        break
    }
  }

  return (
    <div className="tabs">
      <div className="tabs__list" role="tablist" aria-label={label} onKeyDown={onKeyDown}>
        {tabs.map((tab, index) => {
          const isSelected = index === selectedIndex
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              id={`${baseId}-tab-${tab.id}`}
              className={`tabs__tab${isSelected ? ' tabs__tab--selected' : ''}${
                tab.muted === true ? ' tabs__tab--muted' : ''
              }`}
              aria-selected={isSelected}
              aria-controls={`${baseId}-panel-${tab.id}`}
              tabIndex={isSelected ? 0 : -1}
              onClick={() => setSelected(tab.id)}
            >
              {tab.label}
              {tab.count !== undefined ? (
                <span className="tabs__count" aria-label={`${tab.count} items`}>
                  {tab.count}
                </span>
              ) : null}
            </button>
          )
        })}
      </div>
      {activeTab !== undefined ? (
        <div
          className="tabs__panel"
          role="tabpanel"
          id={`${baseId}-panel-${activeTab.id}`}
          aria-labelledby={`${baseId}-tab-${activeTab.id}`}
          tabIndex={0}
        >
          {activeTab.content()}
        </div>
      ) : null}
    </div>
  )
}
