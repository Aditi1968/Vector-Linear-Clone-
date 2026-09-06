import { NavLink } from 'react-router-dom'

import { ChevronRightIcon, Kbd, SearchIcon, cx } from '../../components'
import { useAppPaths } from '../routes/useAppPaths'
import styles from './Sidebar.module.css'

/**
 * The rail's two global entry points: search, and the command palette.
 */

/**
 * Search.
 *
 * A `NavLink` and not a text input. A field in the rail would have to hold
 * query state that survives navigation, submit somewhere, and then decide
 * what to do about the second field on the search screen itself -- so the
 * rail links to the screen and the screen owns the query. It also means the
 * affordance is honest the moment the route exists, with no stub input
 * swallowing what the user types.
 */
export function SearchAffordance() {
  const paths = useAppPaths()

  return (
    <NavLink
      to={paths.search()}
      className={({ isActive }) => cx(styles.action, isActive && styles.actionActive)}
    >
      <SearchIcon className={styles.actionIcon} />
      <span className={cx(styles.actionLabel, styles.collapsible)}>Search</span>
    </NavLink>
  )
}

/**
 * The chord that will open the command palette, spelled for this platform.
 *
 * Read once at module scope from the user agent. `navigator.platform` is the
 * more direct signal and is deprecated in every engine; the user-agent string
 * is the one that still reports a Mac. Getting this wrong shows the wrong
 * glyph in a hint -- which is why a heuristic is acceptable here and would
 * not be for a key binding.
 *
 * Exported so that whoever binds the palette spells the chord the same way
 * the hint does, instead of sniffing the platform a second time.
 */
const IS_APPLE =
  typeof navigator !== 'undefined' && /Mac|iPhone|iPad|iPod/.test(navigator.userAgent)

export const COMMAND_CHORD: readonly [string, string] = IS_APPLE
  ? ['⌘', 'K']
  : ['Ctrl', 'K']

/**
 * The command palette's place in the rail. The palette does not exist yet.
 *
 * `disabled` rather than live-and-inert: a control that opens nothing when
 * pressed is a bug report, and one that is switched off is a statement about
 * the product's state. The accessible name says so outright, because the
 * dimming that says it visually reaches nobody using a screen reader.
 *
 * The chord is still shown, and it is the real one -- it is what a user will
 * reach for the day the palette lands, and a hint is not a promise that the
 * key is bound today. Nothing in this file binds it: a working hint would be
 * the palette, and the palette is another agent's screen.
 */
export function CommandAffordance() {
  return (
    <button
      type="button"
      disabled
      className={cx(styles.action, styles.actionPending)}
      aria-label="Command palette — not available yet"
    >
      <ChevronRightIcon className={styles.actionIcon} />
      <span className={cx(styles.actionLabel, styles.collapsible)}>Command</span>
      <span className={cx(styles.actionHint, styles.collapsible)} aria-hidden="true">
        <Kbd>{COMMAND_CHORD[0]}</Kbd>
        <Kbd>{COMMAND_CHORD[1]}</Kbd>
      </span>
    </button>
  )
}
