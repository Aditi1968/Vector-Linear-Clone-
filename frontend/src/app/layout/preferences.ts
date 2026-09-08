import { useCallback, useEffect, useState, useSyncExternalStore } from 'react'

/**
 * The shell preferences that survive a reload: the theme, whether the rail is
 * collapsed, how tall a list row is, and whether lists are grouped.
 *
 * All of them live in `localStorage` and all are read behind a `try`. Storage
 * *throws* rather than returning null when a browser is set to block site
 * data, and it throws on read as well as on write -- so an unguarded
 * `localStorage.getItem` in a module that every screen mounts under is a
 * blank application for anyone with third-party data disabled. There is no
 * fallback store: a preference that cannot be remembered is a preference that
 * resets, which is a small annoyance rather than a failure.
 */

const THEME_KEY = 'vector.theme'
const SIDEBAR_KEY = 'vector.sidebar.collapsed'
const DENSITY_KEY = 'vector.density'
const GROUP_MODE_KEY = 'vector.list.group'

function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) {
      localStorage.removeItem(key)
    } else {
      localStorage.setItem(key, value)
    }
  } catch {
    // Nothing to do and nothing to report: the user's browser has declined to
    // remember this, which is their call to make.
  }
}

/* ------------------------------------------------------------------ */
/* Theme                                                               */
/* ------------------------------------------------------------------ */

/**
 * `system` follows `prefers-color-scheme`; the other two override it.
 *
 * Three states, not a boolean. A two-way toggle has to pick a starting side,
 * and whichever it picks is wrong for half of the users whose OS is set the
 * other way -- and once toggled there is no way back to "whatever my
 * computer says".
 */
export type ThemePreference = 'system' | 'light' | 'dark'

function isThemePreference(value: string | null): value is ThemePreference {
  return value === 'light' || value === 'dark'
}

/**
 * Put the preference on `<html>`, which is where src/styles/tokens.css looks
 * for it.
 *
 * `system` *removes* the attribute rather than setting it to "system", so the
 * absence of the attribute is what lets the OS preference through; any value
 * at all would defeat it.
 *
 * Vector ships one palette today: tokens.css puts the navy theme on bare
 * `:root` and pins `color-scheme: dark`, so this control has nothing to
 * switch between yet and the attribute it writes is not read by anything.
 * It is kept because the preference is the user's and losing a stored choice
 * is worse than carrying an inert attribute -- and because a light theme, if
 * one is ever designed, arrives as a `:root[data-theme='light']` block and
 * needs no change here.
 */
function apply(preference: ThemePreference): void {
  const root = document.documentElement

  if (preference === 'system') {
    root.removeAttribute('data-theme')
  } else {
    root.setAttribute('data-theme', preference)
  }
}

/** The stored preference, or `system` when there is none. */
export function storedTheme(): ThemePreference {
  const stored = read(THEME_KEY)

  return isThemePreference(stored) ? stored : 'system'
}

/**
 * The theme control's state.
 *
 * The effect applies the preference on mount as well as on change, which is
 * what makes a stored choice survive a reload. It runs after first paint, so
 * a user who chose light on a dark-mode machine sees one dark frame; fixing
 * that properly means a blocking inline script in index.html, which is a
 * change to a file this shell does not own and a flash of the *correct*
 * default is a fair price until then.
 */
export function useTheme(): [ThemePreference, (next: ThemePreference) => void] {
  const [preference, setPreference] = useState<ThemePreference>(storedTheme)

  useEffect(() => {
    apply(preference)
  }, [preference])

  const choose = useCallback((next: ThemePreference) => {
    setPreference(next)
    write(THEME_KEY, next === 'system' ? null : next)
  }, [])

  return [preference, choose]
}

/* ------------------------------------------------------------------ */
/* Sidebar                                                             */
/* ------------------------------------------------------------------ */

/** The width below which the shell stacks. `--layout-breakpoint-compact`. */
const COMPACT_QUERY = '(max-width: 47.999rem)'

/**
 * Whether the rail starts collapsed.
 *
 * A stored choice wins. With nothing stored the answer comes from the
 * viewport: a narrow window gets a collapsed rail, because the alternative is
 * a full navigation stack pushing the actual page off the bottom of a phone
 * screen on first visit. Read once, in a state initialiser, so a later resize
 * never overrides a choice the user has since made.
 */
function initialCollapsed(): boolean {
  const stored = read(SIDEBAR_KEY)

  if (stored === 'true' || stored === 'false') {
    return stored === 'true'
  }

  try {
    return window.matchMedia(COMPACT_QUERY).matches
  } catch {
    return false
  }
}

/** The rail's collapsed state, remembered across reloads. */
export function useSidebarCollapsed(): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState<boolean>(initialCollapsed)

  const toggle = useCallback(() => {
    setCollapsed((wasCollapsed) => {
      write(SIDEBAR_KEY, String(!wasCollapsed))

      return !wasCollapsed
    })
  }, [])

  return [collapsed, toggle]
}

/* ------------------------------------------------------------------ */
/* Density                                                             */
/* ------------------------------------------------------------------ */

/**
 * How tall one list row is.
 *
 * A workspace-wide preference and not a per-screen one, because it is a
 * statement about the user's eyes rather than about the list they happen to
 * be looking at. Someone who wants more rows on screen wants that everywhere.
 */
export type Density = 'dense' | 'comfortable'

function isDensity(value: string | null): value is Density {
  return value === 'dense' || value === 'comfortable'
}

/**
 * Put the choice on `<html>`, which is where src/styles/tokens.css looks.
 *
 * One attribute re-points `--row-height`, and every row in the product
 * follows -- lists, inbox, members, cycles -- because they all measure
 * themselves against that token rather than restating a height each. Nothing
 * re-renders; this is a CSS variable change.
 *
 * `dense` removes the attribute rather than setting it, so the default state
 * of the document is the default density and there is no value to keep in
 * sync with the stylesheet's `:root` block.
 */
function applyDensity(density: Density): void {
  const root = document.documentElement

  if (density === 'dense') {
    root.removeAttribute('data-density')
  } else {
    root.setAttribute('data-density', density)
  }
}

/** The stored density, or `dense` when there is none. */
export function storedDensity(): Density {
  const stored = read(DENSITY_KEY)

  return isDensity(stored) ? stored : 'dense'
}

/** The density control's state, applied to the document and remembered. */
export function useDensity(): [Density, (next: Density) => void] {
  const [density, setDensity] = useState<Density>(storedDensity)

  useEffect(() => {
    applyDensity(density)
  }, [density])

  const choose = useCallback((next: Density) => {
    setDensity(next)
    write(DENSITY_KEY, next === 'dense' ? null : next)
  }, [])

  return [density, choose]
}

/* ------------------------------------------------------------------ */
/* Grouping                                                            */
/* ------------------------------------------------------------------ */

/**
 * Whether a list is one run of rows or a run per workflow state.
 *
 * Unlike density this changes what is rendered, so it is state a screen reads
 * rather than an attribute on the document. It still lives here, and is still
 * global, for the same reason: it is a preference about how someone reads a
 * list, and having it reset every time they move between two lists is the
 * behaviour people describe as "it keeps forgetting".
 *
 * A screen with nothing to group by simply does not offer the control. It
 * must not silently coerce the stored value -- the preference is the user's,
 * and a screen that cannot honour it today should leave it alone rather than
 * write `flat` over a choice every other screen is respecting.
 */
export type GroupMode = 'flat' | 'grouped'

function isGroupMode(value: string | null): value is GroupMode {
  return value === 'flat' || value === 'grouped'
}

/** The stored grouping, or `flat` when there is none. */
export function storedGroupMode(): GroupMode {
  const stored = read(GROUP_MODE_KEY)

  return isGroupMode(stored) ? stored : 'flat'
}

/**
 * Everyone currently reading the grouping, so a change reaches all of them.
 *
 * Two components read this hook and they are in different subtrees: the
 * control lives in the page header's actions and the list that honours it is
 * further down the screen. With `useState` those were two independent copies
 * -- the control moved its own and the list never heard, so the segment
 * flipped and not one pixel of the list changed, which is precisely the
 * failure ViewControls' own documentation warns about.
 *
 * `useSyncExternalStore` over a module-level listener set rather than a
 * context provider: there is nothing to provide. The value already lives
 * outside React, in storage, and a provider would only be a second place to
 * mount before the preference works.
 */
const groupModeListeners = new Set<() => void>()

/**
 * Set only when `localStorage` refused the write.
 *
 * Storage is the store in every ordinary case, which is what keeps the value
 * honest across a reload with nothing to invalidate. A browser set to block
 * site data throws on write, and a control that then did nothing at all would
 * be a broken control rather than an unremembered preference -- so the choice
 * falls back to memory for the life of the page.
 */
let volatileGroupMode: GroupMode | null = null

function currentGroupMode(): GroupMode {
  return volatileGroupMode ?? storedGroupMode()
}

function subscribeGroupMode(listener: () => void): () => void {
  groupModeListeners.add(listener)

  return () => {
    groupModeListeners.delete(listener)
  }
}

/** The grouping control's state, remembered across reloads. */
export function useGroupMode(): [GroupMode, (next: GroupMode) => void] {
  // Same function for the server snapshot: this renders on the client only,
  // and a `flat` server snapshot would guarantee a hydration mismatch for
  // anyone who had chosen `grouped`.
  const mode = useSyncExternalStore(
    subscribeGroupMode,
    currentGroupMode,
    currentGroupMode,
  )

  const choose = useCallback((next: GroupMode) => {
    write(GROUP_MODE_KEY, next === 'flat' ? null : next)
    // Only when the write did not take. Leaving this null in the ordinary
    // case is what stops a stale in-memory value from outliving the storage
    // it was meant to mirror.
    volatileGroupMode = storedGroupMode() === next ? null : next

    for (const listener of groupModeListeners) {
      listener()
    }
  }, [])

  return [mode, choose]
}
