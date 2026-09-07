import { useEffect, useRef, useState } from 'react'

import { Dialog, Input, SearchIcon } from '../../components'
import { matchesChord } from './lib/keyboard'
import type { Chord } from './lib/keyboard'
import styles from './CommandPalette.module.css'

export interface CommandPaletteProps {
  open: boolean
  /** Told when the palette opens or closes, from any path including the chord. */
  onOpenChange: (open: boolean) => void
  /**
   * How this platform spells the chord that opens it.
   *
   * A prop rather than an import. `COMMAND_CHORD` lives in the shell, and the
   * shell is what mounts this component -- importing it back would close a
   * cycle between `app/layout` and this feature, and a module-scope read
   * inside a cycle is a temporal-dead-zone crash rather than a lint warning.
   */
  chord: Chord
}

/**
 * Vector's command palette.
 *
 * ## The dialog is the platform's
 *
 * `Dialog` is `<dialog showModal()>`, which is where the focus trap, the
 * inert background, the top layer and Escape come from. Nothing here
 * reimplements any of that.
 *
 * Two things the platform does *not* do, and this component therefore does:
 *
 *   - **Focus the input.** `showModal()` focuses the first focusable element,
 *     which is the dialog's own close button. A palette that opens with focus
 *     anywhere but the query field is a palette you have to Tab into.
 *   - **Return focus on close.** The browser does this, and jsdom -- which
 *     implements `HTMLDialogElement` without `showModal` -- does not, so
 *     without it the behaviour is untestable *and* absent in the degraded
 *     path `Dialog` falls back to. Restoring focus to an element the browser
 *     has already restored it to is a no-op, so doing it twice is safe.
 *
 * ## The chord is bound here, once
 *
 * The listener is on `window` and lives with the component that owns the
 * palette, rather than in the affordance that also opens it. One binding, so
 * the key and the dialog cannot get out of step.
 */
export function CommandPalette({ open, onOpenChange, chord }: CommandPaletteProps) {
  const [query, setQuery] = useState('')

  const inputRef = useRef<HTMLInputElement>(null)
  const returnFocusTo = useRef<Element | null>(null)

  /**
   * The global chord.
   *
   * A toggle: the chord that opens the palette closes it again, which is what
   * every user who has met one expects, and is one branch rather than two.
   *
   * Deliberately *not* guarded by `isTypingTarget`. A modified chord is not
   * text entry -- Ctrl+K types nothing into a field -- and a palette that
   * refused to open because the caret happened to be in the composer would be
   * broken in exactly the moment someone reaches for it. The single-key
   * shortcuts, which really would type a character, are guarded; see
   * ./shortcuts.ts.
   */
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      // Something nearer the event already dealt with it.
      if (event.defaultPrevented || !matchesChord(event, chord)) {
        return
      }

      // Ctrl+K focuses the address bar in Chrome and Firefox. Taking it is a
      // deliberate trade every product with a palette makes, and it is only
      // taken for the exact chord -- everything else falls through untouched.
      event.preventDefault()
      onOpenChange(!open)
    }

    window.addEventListener('keydown', handleKeyDown)

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [chord, onOpenChange, open])

  /* Focus in on open, and back out on close. See the note above. */
  useEffect(() => {
    if (!open) {
      return undefined
    }

    returnFocusTo.current = document.activeElement
    inputRef.current?.focus()

    return () => {
      const target = returnFocusTo.current

      // `isConnected`, because a command that navigated has unmounted
      // whatever was focused when the palette opened. Focusing a detached
      // node silently moves focus to `<body>`, which loses the caret
      // altogether -- worse than leaving it where the router put it.
      if (target instanceof HTMLElement && target.isConnected) {
        target.focus()
      }
    }
  }, [open])

  /* State from the last time it was open must not be there on the next. Reset
   * on close rather than on open, so a caller that opens the palette into a
   * particular state is not undone by an effect that runs after it. */
  useEffect(() => {
    if (!open) {
      setQuery('')
    }
  }, [open])

  function close() {
    onOpenChange(false)
  }

  return (
    <Dialog
      open={open}
      onClose={close}
      size="lg"
      title="Command palette"
      description="Search this workspace, or run a command."
      className={styles.dialog}
    >
      <div
        className={styles.palette}
        onKeyDown={(event) => {
          /* Escape is the browser's job on a modal `<dialog>` and this is the
           * belt to that pair of braces: it is what closes the palette where
           * `showModal()` is unavailable, and it is idempotent where it is
           * not -- both paths end at the same `onOpenChange(false)`. */
          if (event.key === 'Escape') {
            event.preventDefault()
            close()
          }
        }}
      >
        <Input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
          }}
          icon={<SearchIcon />}
          aria-label="Search issues and projects, or run a command"
          placeholder="Search or jump to..."
          autoComplete="off"
          spellCheck={false}
        />
      </div>
    </Dialog>
  )
}
