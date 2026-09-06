import { useId, useState } from 'react'
import type { ReactNode } from 'react'

import { Dialog, Input, List, ListRow, SearchIcon, Spinner } from '../../../components'
import { MIN_QUERY_LENGTH, useIssueSearch } from '../api'
import type { IssueSearchHit } from '../api'
import styles from '../collaboration.module.css'

export interface IssuePickerProps {
  workspaceSlug: string
  open: boolean
  onClose: () => void
  /** "Add a sub-issue", "Block this issue on". The dialog's accessible name. */
  title: string
  /** One line under the title, explaining what picking will do. */
  description?: string
  /**
   * Issues that must not be offered -- this issue itself, and anything
   * already related to it. Filtered out of the results rather than shown and
   * refused: the server would reject a self-relation anyway, and offering a
   * choice that cannot work is worse than not offering it.
   */
  excludeIds?: readonly string[]
  /** Extra controls above the results, such as a relation-type chooser. */
  children?: ReactNode
  onPick: (issue: IssueSearchHit) => void
  /** Shown inside the dialog when the pick failed. */
  errorMessage?: string | null
  isSubmitting?: boolean
}

/**
 * Choose one issue out of the workspace.
 *
 * A modal dialog, because `<dialog open>` via `showModal()` brings the focus
 * trap, the focus return, Escape-to-dismiss and the top layer with it -- all
 * of which a popover built for this would have to reimplement, and the focus
 * return is the one that always gets skipped.
 *
 * Results are plain buttons in a real list rather than a combobox. A combobox
 * is the right pattern when the control is a text field whose value is the
 * chosen item; here picking is an *action* with a confirmation step behind it
 * (a relation type to choose), the list can be empty or erroring, and a
 * button that says what it will do is both simpler and more honest than
 * `aria-activedescendant` over a listbox.
 */
export function IssuePicker({
  workspaceSlug,
  open,
  onClose,
  title,
  description,
  excludeIds,
  children,
  onPick,
  errorMessage = null,
  isSubmitting = false,
}: IssuePickerProps) {
  const [query, setQuery] = useState('')
  const searchId = useId()
  const { hits, isSearching, errorMessage: searchError, isEmpty } = useIssueSearch(
    workspaceSlug,
    query,
  )

  const excluded = new Set(excludeIds ?? [])
  const results = hits.filter((hit) => !excluded.has(hit.id))

  const close = () => {
    setQuery('')
    onClose()
  }

  return (
    <Dialog open={open} onClose={close} title={title} description={description} size="sm">
      <div className={styles.pickerControls}>
        {children}

        <div className={styles.field}>
          <label htmlFor={searchId} className={styles.fieldLabel}>
            Search issues
          </label>
          <Input
            id={searchId}
            type="search"
            value={query}
            icon={<SearchIcon />}
            placeholder="Title or identifier"
            autoComplete="off"
            aria-describedby={`${searchId}-hint`}
            onChange={(event) => {
              setQuery(event.target.value)
            }}
          />
          <p id={`${searchId}-hint`} className={styles.hint}>
            {`Type at least ${String(MIN_QUERY_LENGTH)} characters.`}
          </p>
        </div>
      </div>

      {/* One region for every transient thing the search has to say, so the
        * three of them cannot be announced over each other. */}
      <div role="status" aria-live="polite" className={styles.announcement}>
        {isSearching ? 'Searching' : isEmpty ? 'No issues match that search' : null}
      </div>

      {searchError !== null && (
        <p role="alert" className={styles.formError}>
          {searchError}
        </p>
      )}

      {errorMessage !== null && (
        <p role="alert" className={styles.formError}>
          {errorMessage}
        </p>
      )}

      {isSearching && (
        <div className={styles.pickerBusy}>
          <Spinner />
        </div>
      )}

      {results.length > 0 && (
        <List label="Search results" className={styles.pickerResults}>
          {results.map((hit) => (
            <ListRow key={hit.id} interactive>
              <button
                type="button"
                className={styles.pickerResult}
                disabled={isSubmitting}
                onClick={() => {
                  onPick(hit)
                }}
              >
                <span className={styles.identifier}>{hit.identifier}</span>
                <span className={styles.pickerResultTitle}>{hit.title}</span>
              </button>
            </ListRow>
          ))}
        </List>
      )}
    </Dialog>
  )
}
