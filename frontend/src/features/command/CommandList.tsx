import { EmptyState, ErrorState, SearchIcon, Spinner, cx } from '../../components'
import type { PaletteGroup, PaletteItem } from './commands'
import styles from './CommandPalette.module.css'

export interface CommandListProps {
  groups: readonly PaletteGroup[]
  /** The DOM id the combobox names in `aria-controls`. */
  id: string
  /** Prefix for every option's id, so the combobox can point at one. */
  optionIdPrefix: string
  activeItemId: string | null
  /** A search is in flight, or about to be. Suppresses "no matches". */
  loading: boolean
  /** The search failed. */
  failed: boolean
  onActivate: (item: PaletteItem) => void
  onHover: (item: PaletteItem) => void
}

/** The DOM id of one option. Built in one place, read from two. */
export function optionDomId(prefix: string, itemId: string): string {
  return `${prefix}-${itemId}`
}

/**
 * The results, as a listbox.
 *
 * ## Why `aria-activedescendant` rather than roving tabindex
 *
 * The user is typing. Focus has to stay in the text field for the next
 * keystroke to land there, so the cursor through the results cannot *be*
 * focus -- it is a pointer the field carries to an option that never takes
 * focus itself. That is the case `aria-activedescendant` exists for, and it
 * is the pattern ARIA specifies for a combobox with a listbox popup.
 *
 * A roving tabindex would be the right answer for a list you arrow through
 * with no text field involved -- which is what the issue list does, and why
 * it moves real focus instead.
 *
 * ## Groups
 *
 * `role="group"` inside the listbox, each named by its own heading through
 * `aria-labelledby`, so a screen reader announces "Issues, 3 items" as the
 * cursor crosses into it instead of reading twenty undifferentiated options.
 * The headings are plain elements: giving them a role would make them
 * something a listbox is not allowed to contain.
 */
export function CommandList({
  groups,
  id,
  optionIdPrefix,
  activeItemId,
  loading,
  failed,
  onActivate,
  onHover,
}: CommandListProps) {
  if (failed) {
    /* No `detail`, and no retry button. What went wrong is the transport or
     * the server, neither of which has anything a user can act on, and the
     * next keystroke reissues the search anyway. */
    return (
      <ErrorState
        title="Could not search this workspace"
        description="Something went wrong on our side. Try again in a moment."
      />
    )
  }

  if (groups.length === 0) {
    /*
      The order of these two matters. A palette that says "no matches" while
      the request is still out is telling the user the answer is no, and they
      will act on it -- so the wait is shown until there is a real answer.

      The spinner is unlabelled decoration: the palette's own status region
      says "Searching", and two elements announcing one wait is worse than
      one.
    */
    return (
      <div className={styles.pending}>
        {loading ? (
          <Spinner />
        ) : (
          <EmptyState
            icon={<SearchIcon />}
            title="No matches"
            description="Nothing in this workspace matches that. Try fewer words."
          />
        )}
      </div>
    )
  }

  return (
    <div id={id} role="listbox" aria-label="Results and commands" className={styles.listbox}>
      {groups.map((group) => (
        <div
          key={group.id}
          role="group"
          aria-labelledby={`${optionIdPrefix}-group-${group.id}`}
          className={styles.group}
        >
          <div id={`${optionIdPrefix}-group-${group.id}`} className={styles.groupLabel}>
            {group.label}
          </div>

          {group.items.map((item) => (
            <div
              key={item.id}
              id={optionDomId(optionIdPrefix, item.id)}
              role="option"
              aria-selected={item.id === activeItemId}
              className={cx(styles.option, item.id === activeItemId && styles.optionActive)}
              /* No `tabIndex`. An option here must never take focus: the
               * query field holds it, and a click that moved focus out of the
               * field would break the next keystroke. */
              onClick={() => {
                onActivate(item)
              }}
              onMouseMove={() => {
                onHover(item)
              }}
            >
              <span className={styles.optionIcon} aria-hidden="true">
                {item.icon}
              </span>
              <span className={styles.optionLabel}>{item.label}</span>
              {item.hint !== undefined && (
                <span className={styles.optionHint}>{item.hint}</span>
              )}
            </div>
          ))}
        </div>
      ))}
    </div>
  )
}
