import { useEffect, useRef } from 'react'

import { Kbd } from '../../components'
import type { Chord } from './lib/keyboard'
import styles from './CommandPalette.module.css'

export interface ShortcutsViewProps {
  /** The palette's chord, spelled for this platform. */
  chord: Chord
  /** Whether the mounted screen offers a composer, and `C` therefore works. */
  canCreateIssue: boolean
}

interface Shortcut {
  /** One `<kbd>` per entry. Words rather than glyphs -- see below. */
  keys: readonly string[]
  description: string
}

interface ShortcutSection {
  title: string
  shortcuts: readonly Shortcut[]
}

/**
 * Every shortcut in Vector, and nothing else.
 *
 * The rule this list is under is the same one the palette is under: what is
 * listed must work. A reference that documents a key nobody bound is worse
 * than no reference, because it is believed -- so `C` is here only when a
 * screen has actually offered a composer, and the list-level keys are filed
 * under the screen they work on rather than under "anywhere".
 *
 * Words, not glyphs, in the keycaps. `↑` is read aloud as "up arrow" by some
 * screen readers, as "upwards arrow" by others and skipped entirely by a
 * third; "Up" is read the same way by all of them. The palette's own footer
 * uses the glyphs, and it is `aria-hidden`, which is the trade the other way
 * around.
 */
function sectionsFor(chord: Chord, canCreateIssue: boolean): readonly ShortcutSection[] {
  const global: Shortcut[] = [
    { keys: [chord[0], chord[1]], description: 'Open the command palette' },
    { keys: ['/'], description: 'Search this workspace' },
    { keys: ['?'], description: 'Show this list' },
  ]

  if (canCreateIssue) {
    global.push({ keys: ['C'], description: 'New issue' })
  }

  return [
    { title: 'Anywhere', shortcuts: global },
    {
      title: 'In the command palette',
      shortcuts: [
        { keys: ['Up', 'Down'], description: 'Move between results' },
        { keys: ['Enter'], description: 'Run the highlighted result' },
        { keys: ['Esc'], description: 'Close the palette' },
      ],
    },
    {
      title: 'On the issue list',
      shortcuts: [
        { keys: ['Up', 'Down'], description: 'Move between rows' },
        { keys: ['Enter'], description: 'Open the focused issue' },
        { keys: ['Esc'], description: 'Close the open issue' },
      ],
    },
  ]
}

/**
 * The keyboard reference, shown inside the palette rather than beside it.
 *
 * One dialog and not two. A second `<dialog>` opened over the first is a
 * second focus trap, a second Escape target and a second thing to close in
 * the right order; swapping what this one contains costs none of that, and
 * the dialog's accessible name changes with it so a screen-reader user is
 * told where they now are.
 */
export function ShortcutsView({ chord, canCreateIssue }: ShortcutsViewProps) {
  const containerRef = useRef<HTMLDivElement>(null)

  /*
    The query field that had focus has just unmounted, and focus with it --
    it would land on `<body>`, outside the dialog, where Escape no longer
    reaches this component. Moving it to the container keeps the keystroke
    inside and gives a screen reader something to start reading from.
  */
  useEffect(() => {
    containerRef.current?.focus()
  }, [])

  return (
    <div ref={containerRef} tabIndex={-1} className={styles.shortcuts}>
      {sectionsFor(chord, canCreateIssue).map((section) => (
        <section key={section.title}>
          <h3 className={styles.shortcutsHeading}>{section.title}</h3>

          <dl className={styles.shortcutList}>
            {section.shortcuts.map((shortcut) => (
              <div key={shortcut.description} className={styles.shortcutRow}>
                <dt className={styles.shortcutKeys}>
                  {shortcut.keys.map((key) => (
                    <Kbd key={key}>{key}</Kbd>
                  ))}
                </dt>
                <dd className={styles.shortcutDescription}>{shortcut.description}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
    </div>
  )
}
