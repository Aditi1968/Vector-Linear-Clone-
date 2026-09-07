import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'

import { Dialog, Input, Kbd, SearchIcon, Spinner, VisuallyHidden } from '../../components'
import { CommandList, optionDomId } from './CommandList'
import { filterItems, useNavigationCommands } from './commands'
import type { PaletteGroup, PaletteItem } from './commands'
import { useCommandSearch } from './useCommandSearch'
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
 *
 * ## Only real commands
 *
 * Every entry navigates somewhere that exists or runs something that works.
 * Nothing is listed as unavailable, greyed out or coming soon: a palette is a
 * promise that what it shows can be done, and an entry that cannot is worse
 * than an entry that is missing.
 */
export function CommandPalette({ open, onOpenChange, chord }: CommandPaletteProps) {
  const [query, setQuery] = useState('')

  /**
   * Which option the cursor is on, as an index into the flattened list.
   *
   * An index rather than an id, because the list is rebuilt as the query
   * changes and an id can vanish under the cursor; an index that runs off the
   * end is clamped below and always names *something*. The id is derived
   * from it for the ARIA attribute, never stored.
   */
  const [activeIndex, setActiveIndex] = useState(0)

  const inputRef = useRef<HTMLInputElement>(null)
  const returnFocusTo = useRef<Element | null>(null)

  const baseId = useId()
  const listboxId = `${baseId}-listbox`

  const navigationCommands = useNavigationCommands()
  const search = useCommandSearch(query)

  /**
   * What the workspace holds first, what the product can do second.
   *
   * The order is the answer to "what did the user mean". Someone who types
   * `auth` almost always wants the issue about authentication, not the
   * Settings screen -- and the commands are a short, stable list that stays
   * reachable one arrow-up away because the cursor wraps.
   */
  const groups = useMemo<readonly PaletteGroup[]>(
    () =>
      [
        { id: 'issues', label: 'Issues', items: search.issues },
        { id: 'projects', label: 'Projects', items: search.projects },
        { id: 'navigation', label: 'Go to', items: filterItems(navigationCommands, query) },
      ].filter((group) => group.items.length > 0),
    [navigationCommands, query, search.issues, search.projects],
  )

  const items = useMemo(() => groups.flatMap((group) => group.items), [groups])

  /* Clamped rather than corrected in an effect. Results arrive asynchronously
   * and a state update that chased them would render one frame with an index
   * pointing past the end -- which is the frame `aria-activedescendant` names
   * an element that is not there. */
  const activeItem: PaletteItem | null =
    items.length === 0 ? null : (items[Math.min(activeIndex, items.length - 1)] ?? null)

  /**
   * The global chord.
   *
   * A toggle: the chord that opens the palette closes it again, which is what
   * every user who has met one expects, and is one branch rather than two.
   *
   * Deliberately *not* guarded by `isTypingTarget`. A modified chord is not
   * text entry -- Ctrl+K types nothing into a field -- and a palette that
   * refused to open because the caret happened to be in the composer would be
   * broken in exactly the moment someone reaches for it. Single-key shortcuts
   * really would type a character, and those are guarded.
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
      setActiveIndex(0)
    }
  }, [open])

  /**
   * Keep the cursor in view.
   *
   * Optional-called: `scrollIntoView` is part of every browser and part of no
   * headless DOM, so an unguarded call turns "arrow past the fold" into a
   * crash in the test environment and nowhere else.
   */
  useEffect(() => {
    if (activeItem === null) {
      return
    }

    const element = document.getElementById(optionDomId(baseId, activeItem.id))

    element?.scrollIntoView?.({ block: 'nearest' })
  }, [activeItem, baseId])

  function close() {
    onOpenChange(false)
  }

  function run(item: PaletteItem) {
    item.run()

    if (item.keepOpen !== true) {
      close()
    }
  }

  /**
   * Move the cursor, wrapping at both ends.
   *
   * Wrapping is right here and wrong on the issue list. There, arrowing past
   * the last row falls through to the browser and scrolls the page, which is
   * what the keystroke means in a long document. Here the list is short, the
   * container is the whole surface, and Up from the first item to reach the
   * last is the fastest route to it.
   */
  function moveActive(delta: number) {
    if (items.length === 0) {
      return
    }

    const from = Math.min(activeIndex, items.length - 1)

    setActiveIndex((from + delta + items.length) % items.length)
  }

  function handleKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        moveActive(1)
        break

      case 'ArrowUp':
        event.preventDefault()
        moveActive(-1)
        break

      case 'Enter':
        if (activeItem !== null) {
          event.preventDefault()
          run(activeItem)
        }
        break

      case 'Escape':
        /* Escape is the browser's job on a modal `<dialog>`, and this is the
         * belt to those braces: it is what closes the palette where
         * `showModal()` is unavailable, and it is idempotent where it is
         * not -- both paths end at the same `onOpenChange(false)`. */
        event.preventDefault()
        close()
        break

      default:
        break
    }
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
      {/* One handler, on the container rather than on the field, so that the
        * keys still work when focus is on the dialog's own close button --
        * which is where Shift+Tab from the field lands. Nothing here is
        * focusable that was not already. */}
      <div className={styles.palette} onKeyDown={handleKeyDown}>
        <Input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value)
            // A new query is a new list. Starting anywhere but the top would
            // put the cursor on whatever happened to land at the old index.
            setActiveIndex(0)
          }}
          /* The glyph becomes the spinner while a search is out. It is the
           * one place in the palette that never moves, so a wait shown here
           * costs no layout and cannot push the results the user is reading.
           * Unlabelled: the status region below announces the wait. */
          icon={search.loading ? <Spinner /> : <SearchIcon />}
          role="combobox"
          aria-label="Search issues and projects, or run a command"
          aria-controls={listboxId}
          aria-expanded={items.length > 0}
          aria-autocomplete="list"
          aria-activedescendant={
            activeItem === null ? undefined : optionDomId(baseId, activeItem.id)
          }
          placeholder="Search or jump to..."
          autoComplete="off"
          spellCheck={false}
        />

        <CommandList
          groups={groups}
          id={listboxId}
          optionIdPrefix={baseId}
          activeItemId={activeItem?.id ?? null}
          loading={search.loading}
          failed={search.failed}
          onActivate={run}
          onHover={(item) => {
            setActiveIndex(items.indexOf(item))
          }}
        />

        {/*
          The count, announced but not drawn.

          `role="status"` is polite: it waits for the screen reader to finish
          its sentence rather than cutting across the letter the user just
          typed. Without it, a sighted user watches the list change and
          everyone else gets silence.
        */}
        <div role="status" aria-live="polite">
          <VisuallyHidden as="div">
            {search.loading
              ? 'Searching'
              : items.length === 0
                ? 'No results'
                : `${String(items.length)} ${items.length === 1 ? 'result' : 'results'}`}
          </VisuallyHidden>
        </div>

        <div className={styles.footer} aria-hidden="true">
          <span>
            <Kbd>↑</Kbd>
            <Kbd>↓</Kbd> to move
          </span>
          <span>
            <Kbd>↵</Kbd> to run
          </span>
          <span>
            <Kbd>Esc</Kbd> to close
          </span>
        </div>
      </div>
    </Dialog>
  )
}
