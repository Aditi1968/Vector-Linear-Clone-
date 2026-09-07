/**
 * The command palette's public surface.
 *
 * One component and the ARIA spelling of the chord that opens it. The shell
 * renders both -- `app/layout/ShellActions.tsx` -- and nothing else in the
 * product needs anything from this feature.
 *
 * Nothing here imports from `app/layout`, and that is a constraint rather
 * than a coincidence: the shell imports this, so an import back would be a
 * cycle. Everything the palette needs from the shell (the chord, the
 * create-issue action) arrives as a prop.
 */

export { CommandPalette } from './CommandPalette'
export type { CommandPaletteProps } from './CommandPalette'

export { ariaKeyshortcuts } from './lib/keyboard'
export type { Chord } from './lib/keyboard'
