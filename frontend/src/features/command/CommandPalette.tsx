import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'

import {
  Dialog,
  Input,
  Kbd,
  PlusIcon,
  SearchIcon,
  SettingsIcon,
  Spinner,
  VisuallyHidden,
} from '../../components'
import { CommandList, optionDomId } from './CommandList'
import { ShortcutsView } from './ShortcutsView'
import { filterItems, useNavigationCommands } from './commands'
import type { PaletteGroup, PaletteItem } from './commands'
import { useCommandSearch } from './useCommandSearch'
import { isPlainKey, matchesChord } from './lib/keyboard'
import type { Chord } from './lib/keyboard'
import styles from './CommandPalette.module.css'

/**
 * What the palette is showing.
 *
 * The keyboard reference lives inside the palette rather than in a dialog of
 * its own: a second `<dialog>` over the first is a second focus trap and a
 * second Escape target, and swapping the contents of one costs neither.
 */
type PaletteView = 'commands' | 'shortcuts'

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
  /**
   * The mounted screen's issue composer, or `null` when it offers none.
   *
   * The shell's create-issue slot, read by the shell and passed in -- the
   * same one the rail's "New issue" button uses, so there is one create path
   * and not a second one the palette invented. `null` removes the command
   * rather than disabling it: a palette entry that cannot run is worse than
   * an entry that is not there, because it costs an arrow key and an Enter
   * to discover.
   */
  createIssue: (() => void) | null
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
export function CommandPalette({
  open,
  onOpenChange,
  chord,
  createIssue,
}: CommandPaletteProps) {
  const [query, setQuery] = useState('')
  const [view, setView] = useState<PaletteView>('commands')

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

  /* Every action the palette can run. "New issue" is here only when a mounted
   * screen has actually offered a composer. */
  const actionCommands = useMemo<readonly PaletteItem[]>(() => {
    const actions: PaletteItem[] = []

    if (createIssue !== null) {
      actions.push({
        id: 'new-issue',
        label: 'New issue',
        icon: <PlusIcon />,
        run: createIssue,
      })
    }

    actions.push({
      id: 'shortcuts',
      label: 'Keyboard shortcuts',
      icon: <SettingsIcon />,
      // The one command that does not dismiss: it replaces what the palette
      // is showing, so closing would undo the thing it just did.
      keepOpen: true,
      run: () => {
        setView('shortcuts')
      },
    })

    return actions
  }, [createIssue])

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
        { id: 'actions', label: 'Actions', items: filterItems(actionCommands, query) },
      ].filter((group) => group.items.length > 0),
    [actionCommands, navigationCommands, query, search.issues, search.projects],
  )

  const items = useMemo(() => groups.flatMap((group) => group.items), [groups])

  /* Clamped rather than corrected in an effect. Results arrive asynchronously
   * and a state update that chased them would render one frame with an index
   * pointing past the end -- which is the frame `aria-activedescendant` names
   * an element that is not there. */
  const activeItem: PaletteItem | null =
    items.length === 0 ? null : (items[Math.min(activeIndex, items.length - 1)] ?? null)

  /**
   * Vector's product-wide keys, bound once, here.
   *
   * ## The chord
   *
   * A toggle: the chord that opens the palette closes it again, which is what
   * every user who has met one expects, and is one branch rather than two.
   *
   * Deliberately *not* guarded by `isTypingTarget`. A modified chord is not
   * text entry -- Ctrl+K types nothing into a field -- and a palette that
   * refused to open because the caret happened to be in the composer would be
   * broken in exactly the moment someone reaches for it.
   *
   * ## The single keys
   *
   * `/`, `?` and `c` all *are* characters, so every one of them goes through
   * `isPlainKey`, which rejects them when a modifier is held and when the
   * target is an input, a textarea, a select or anything `contenteditable`.
   * That guard is `features/issues/lib/keyboard.ts` -- the module the issue
   * list already uses for its own `c` -- rather than a second list of what
   * counts as a text field, because two such lists disagree the first time
   * one of them learns about a new element.
   *
   * `c` is bound to the shell's create-issue slot, so it works on every
   * screen that offers a composer rather than only on the issue list. The
   * issue list binds `c` too; this listener is registered first (the rail
   * renders above the outlet) and calls `preventDefault`, which is the
   * condition that screen's handler checks before doing anything. Both paths
   * end at the same `openComposer` either way, so the ordering is a tidiness
   * property and not a correctness one.
   */
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      // Something nearer the event already dealt with it.
      if (event.defaultPrevented) {
        return
      }

      if (matchesChord(event, chord)) {
        // Ctrl+K focuses the address bar in Chrome and Firefox. Taking it is
        // a deliberate trade every product with a palette makes, and it is
        // taken only for the exact chord -- everything else falls through.
        event.preventDefault()
        onOpenChange(!open)
        return
      }

      // While the palette is open its own handler owns every key. Nothing
      // below would reach a typing target anyway, but `/` typed into the
      // query field is a search term and must stay one.
      if (open) {
        return
      }

      if (isPlainKey(event, '/')) {
        // `/` is Firefox's quick-find. Taken for the same reason every
        // search-first product takes it, and only outside a text field --
        // where quick-find is what the user meant, they are not in one.
        event.preventDefault()
        setView('commands')
        onOpenChange(true)
        return
      }

      if (isPlainKey(event, '?')) {
        event.preventDefault()
        setView('shortcuts')
        onOpenChange(true)
        return
      }

      if (createIssue !== null && (isPlainKey(event, 'c') || isPlainKey(event, 'C'))) {
        event.preventDefault()
        createIssue()
      }
    }

    window.addEventListener('keydown', handleKeyDown)

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [chord, createIssue, onOpenChange, open])

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
      setView('commands')
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
    /* The reference has no cursor to move and nothing to run. Escape still
     * closes, which is why the guard is here and not around the handler. */
    const inList = view === 'commands'

    switch (event.key) {
      case 'ArrowDown':
        if (inList) {
          event.preventDefault()
          moveActive(1)
        }
        break

      case 'ArrowUp':
        if (inList) {
          event.preventDefault()
          moveActive(-1)
        }
        break

      case 'Enter':
        if (inList && activeItem !== null) {
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
      /* The name changes with the view, so a screen reader announces where
       * the user now is rather than leaving them in a dialog called
       * "Command palette" that is showing a reference table. */
      title={view === 'shortcuts' ? 'Keyboard shortcuts' : 'Command palette'}
      description={
        view === 'shortcuts'
          ? 'Every key Vector binds. Escape closes.'
          : 'Search this workspace, or run a command.'
      }
      className={styles.dialog}
    >
      {/* One handler, on the container rather than on the field, so that the
        * keys still work when focus is on the dialog's own close button --
        * which is where Shift+Tab from the field lands. Nothing here is
        * focusable that was not already. */}
      <div className={styles.palette} onKeyDown={handleKeyDown}>
        {view === 'shortcuts' && (
          <ShortcutsView chord={chord} canCreateIssue={createIssue !== null} />
        )}

        {view === 'commands' && (
          <>
            <Input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(event) => {
                setQuery(event.target.value)
                // A new query is a new list. Starting anywhere but the top
                // would put the cursor on whatever landed at the old index.
                setActiveIndex(0)
              }}
              /* The glyph becomes the spinner while a search is out. It is
               * the one place in the palette that never moves, so a wait
               * shown here costs no layout and cannot push the results the
               * user is reading. Unlabelled: the status region announces it. */
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

              `role="status"` is polite: it waits for the screen reader to
              finish its sentence rather than cutting across the letter the
              user just typed. Without it, a sighted user watches the list
              change and everyone else gets silence.
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
          </>
        )}
      </div>
    </Dialog>
  )
}
