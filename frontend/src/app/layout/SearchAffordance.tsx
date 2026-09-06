import { SearchIcon } from '../../components'
import styles from './SearchAffordance.module.css'

/**
 * The place search will go. Search does not exist.
 *
 * There is no search field, no search query, no search index and no search
 * resolver anywhere in Vector's backend. This component therefore has one
 * job: reserve the position in the shell's layout without ever implying that
 * typing into it would do something.
 *
 * How it avoids implying that, in four independent ways -- independent so
 * that no single user's configuration removes all of them:
 *
 *   1. It is a `<button disabled>`, not an `<input>`. A disabled input still
 *      *looks* typeable and some browsers still show a text caret over it;
 *      a disabled button cannot be focused, cannot be typed into, and is
 *      removed from the tab order, so no keyboard user can land on it and
 *      wonder why nothing happens.
 *   2. It is visibly dashed and dimmed, a treatment used nowhere else in the
 *      product.
 *   3. It carries the visible word "Soon", which survives greyscale, dark
 *      mode, forced-colours mode, and a user stylesheet -- none of which the
 *      dimming survives.
 *   4. Its accessible name says so outright.
 *
 * The accessible name begins with the visible word "Search" so that WCAG
 * 2.5.3 (Label in Name) holds -- a speech-input user saying "click Search"
 * still matches this control -- and then states the part the visual design
 * conveys through styling.
 *
 * There is no `onClick`, no handler, and no fake result list. When search is
 * built it replaces this file; nothing else in the shell changes.
 */
export function SearchAffordance() {
  return (
    <button
      type="button"
      disabled
      className={styles.search}
      aria-label="Search — not available yet"
    >
      <SearchIcon className={styles.icon} />
      <span className={styles.label}>Search</span>
      <span className={styles.badge}>Soon</span>
    </button>
  )
}
