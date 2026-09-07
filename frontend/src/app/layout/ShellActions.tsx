import { useState } from 'react'
import { NavLink } from 'react-router-dom'

import { ChevronRightIcon, Kbd, SearchIcon, cx } from '../../components'
import { CommandPalette, ariaKeyshortcuts } from '../../features/command'
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
 * The command palette's place in the rail, and the palette itself.
 *
 * Both here, and the state that joins them is a `useState` rather than a
 * context. The palette is a `<dialog>` in the browser's top layer, so where
 * it sits in the tree decides nothing about where it paints -- which means
 * the smallest correct home for it is beside the one control that opens it.
 * A provider in `AppLayout` would buy nothing and would be a second thing to
 * keep in step.
 *
 * The chord is passed down rather than imported by the palette: the shell
 * imports `features/command`, so an import back would close a cycle.
 */
export function CommandAffordance() {
  const [open, setOpen] = useState(false)

  return (
    <>
      <button
        type="button"
        className={styles.action}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-keyshortcuts={ariaKeyshortcuts(COMMAND_CHORD)}
        onClick={() => {
          setOpen(true)
        }}
      >
        <ChevronRightIcon className={styles.actionIcon} />
        <span className={cx(styles.actionLabel, styles.collapsible)}>Command</span>
        {/* `aria-hidden`, because `aria-keyshortcuts` above already says this
          * to a screen reader in the spelling the spec defines. Two
          * announcements of one shortcut is worse than one. */}
        <span className={cx(styles.actionHint, styles.collapsible)} aria-hidden="true">
          <Kbd>{COMMAND_CHORD[0]}</Kbd>
          <Kbd>{COMMAND_CHORD[1]}</Kbd>
        </span>
      </button>

      <CommandPalette open={open} onOpenChange={setOpen} chord={COMMAND_CHORD} />
    </>
  )
}
