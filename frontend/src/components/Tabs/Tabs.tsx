import { useId, useRef } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'

import { cx } from '../cx'
import styles from './Tabs.module.css'

export interface TabItem {
  id: string
  label: string
  /** A count shown as a pill after the label -- open issues, comments. */
  count?: number
  content: ReactNode
}

export interface TabsProps {
  /** Names the tablist. Required: "Tabs" alone tells a screen reader nothing. */
  label: string
  tabs: readonly TabItem[]
  /** The selected tab's id. Controlled, so selection can live in the URL. */
  value: string
  onChange: (id: string) => void
  className?: string
}

/**
 * A tab set.
 *
 * Controlled rather than self-managing, because which tab is open is usually
 * part of the route in a product like this -- and a component that owns that
 * state internally is one you then have to fight to deep-link into.
 *
 * Keyboard contract (WAI-ARIA tabs, automatic activation):
 *   ArrowLeft / ArrowRight   previous / next tab, wrapping
 *   Home / End               first / last tab
 *   Tab                      leaves the tablist and lands in the panel
 *
 * Automatic activation -- selecting as focus moves -- is the right choice here
 * because the panels are already rendered and switching costs nothing. Manual
 * activation exists for tabs whose panels are expensive to load, and using it
 * where it is not needed just means every keyboard user presses one more key.
 *
 * Only the selected tab is tabbable (roving `tabindex`). A tablist of eight
 * tabs should be one Tab stop, not eight.
 */
export function Tabs({ label, tabs, value, onChange, className }: TabsProps) {
  const baseId = useId()
  const listRef = useRef<HTMLDivElement>(null)

  const tabId = (id: string) => `${baseId}-tab-${id}`
  const panelId = (id: string) => `${baseId}-panel-${id}`

  const selectAt = (index: number) => {
    const wrapped = (index + tabs.length) % tabs.length
    const next = tabs[wrapped]
    if (next === undefined) return
    onChange(next.id)
    // Focus follows selection, otherwise the ring is left on a tab that is no
    // longer the selected one and the next arrow press starts from the wrong
    // place.
    //
    // Found by position rather than by id: `useId` emits colons and guillemets
    // that are legal in an HTML id but not in a CSS selector, and the DOM order
    // of the buttons is the render order of `tabs` by construction.
    listRef.current
      ?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
      .item(wrapped)
      .focus()
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const current = tabs.findIndex((tab) => tab.id === value)

    switch (event.key) {
      case 'ArrowRight':
        event.preventDefault()
        selectAt(current + 1)
        break
      case 'ArrowLeft':
        event.preventDefault()
        selectAt(current - 1)
        break
      case 'Home':
        event.preventDefault()
        selectAt(0)
        break
      case 'End':
        event.preventDefault()
        selectAt(tabs.length - 1)
        break
      default:
        break
    }
  }

  const selected = tabs.find((tab) => tab.id === value)

  return (
    <div className={className}>
      <div
        ref={listRef}
        role="tablist"
        aria-label={label}
        className={styles.tablist}
        onKeyDown={handleKeyDown}
      >
        {tabs.map((tab) => {
          const isSelected = tab.id === value
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              id={tabId(tab.id)}
              aria-selected={isSelected}
              aria-controls={panelId(tab.id)}
              tabIndex={isSelected ? 0 : -1}
              className={cx(styles.tab, isSelected && styles.selected)}
              onClick={() => {
                onChange(tab.id)
              }}
            >
              {tab.label}
              {tab.count !== undefined && (
                <span className={styles.count}>{tab.count}</span>
              )}
            </button>
          )
        })}
      </div>

      {selected !== undefined && (
        <div
          role="tabpanel"
          id={panelId(selected.id)}
          aria-labelledby={tabId(selected.id)}
          /* Focusable so that Tab out of the tablist lands on the panel. If the
           * panel's own content is focusable this is redundant, but there is no
           * way to know that from here and an unreachable panel is the worse
           * failure. */
          tabIndex={0}
          className={styles.panel}
        >
          {selected.content}
        </div>
      )}
    </div>
  )
}
