import { useCallback, useEffect, useState } from 'react'

/**
 * The two shell preferences that survive a reload: the theme, and whether the
 * rail is collapsed.
 *
 * Both live in `localStorage` and both are read behind a `try`. Storage
 * *throws* rather than returning null when a browser is set to block site
 * data, and it throws on read as well as on write -- so an unguarded
 * `localStorage.getItem` in a module that every screen mounts under is a
 * blank application for anyone with third-party data disabled. There is no
 * fallback store: a preference that cannot be remembered is a preference that
 * resets, which is a small annoyance rather than a failure.
 */

const THEME_KEY = 'vector.theme'
const SIDEBAR_KEY = 'vector.sidebar.collapsed'

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
 * `system` *removes* the attribute rather than setting it to "system".
 * tokens.css defines dark under `@media (prefers-color-scheme: dark)` guarded
 * by `:root:not([data-theme='light'])`, so the absence of the attribute is
 * what lets the OS preference through; any value at all would defeat it.
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
