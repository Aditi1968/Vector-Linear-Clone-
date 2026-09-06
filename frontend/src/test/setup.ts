/**
 * Per-file test setup.
 *
 * Two jobs, and both are needed because this suite runs without Vitest's
 * `globals` option.
 */

import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// Registers `toBeInTheDocument`, `toBeDisabled`, `toHaveAccessibleName` and
// the rest on Vitest's `expect`. The `/vitest` entry point also declares the
// module augmentation that types them, which is why it is imported here (a
// file inside `src`, and therefore inside tsconfig.app.json's program) rather
// than being listed only as a side-effect import in some test file.
import '@testing-library/jest-dom/vitest'

/**
 * Unmount anything a test rendered.
 *
 * Testing Library registers this itself when `afterEach` is a global. It is
 * not one here, so it is registered explicitly. Without it, every rendered
 * tree stays mounted for the rest of the file: `screen` queries the whole
 * document and would start matching elements from a previous test, which
 * fails in the confusing direction -- a query finding *two* matches rather
 * than none.
 */
afterEach(() => {
  cleanup()

  /*
    The shell remembers two things across a reload -- the theme and whether
    the rail is collapsed -- and `localStorage` and `<html>` are the two
    pieces of state in this environment that `cleanup()` does not touch. One
    test collapsing the rail would otherwise leave every test that ran after
    it mounting a collapsed rail, which fails in the confusing direction: a
    control found by name in one file and not in the next, depending on
    ordering.

    Cleared rather than stubbed, so the persistence itself is still the real
    thing under test -- `shell.test.tsx` mounts twice in one test to prove a
    choice survives, and that only means something against real storage.
  */
  try {
    localStorage.clear()
  } catch {
    // A browser that refuses storage has nothing to clear.
  }

  document.documentElement.removeAttribute('data-theme')
})
