/**
 * Matching a keystroke against a shortcut.
 *
 * The other half of this problem -- whether a keystroke belongs to something
 * the user is typing into -- is already solved in
 * `features/issues/lib/keyboard.ts`, and is imported from there rather than
 * written a second time. One list of "what counts as a text field" is the
 * whole point: a second copy is a copy that will disagree the first time
 * somebody adds an element to one of them.
 */

import { isTypingTarget } from '../../issues/lib/keyboard'

export { isTypingTarget }

/**
 * The chord, as the shell spells it for this platform.
 *
 * `['⌘', 'K']` on a Mac, `['Ctrl', 'K']' everywhere else. The shell exports
 * the value (see `app/layout`), and this module reads the platform back out
 * of it rather than sniffing the user agent a second time -- so the key the
 * hint advertises and the key that is bound cannot drift apart.
 */
export type Chord = readonly [string, string]

/** The Apple spelling of the modifier, and the only place it is written. */
const APPLE_MODIFIER = '⌘'

/**
 * Does this keystroke *invoke* the chord?
 *
 * Strict about which modifier, per platform. Accepting Ctrl on a Mac as well
 * would be friendlier right up until it swallowed Ctrl+K in a text field,
 * which is "kill to end of line" in every macOS text control; and Cmd+K on
 * Windows is not a key anyone presses. So the platform the hint was spelled
 * for is the platform the binding matches.
 *
 * Alt disqualifies. Ctrl+Alt is how AltGr arrives on a Windows keyboard, so
 * an unguarded Ctrl match would steal a character from anyone whose layout
 * puts it there.
 */
export function matchesChord(event: KeyboardEvent, chord: Chord): boolean {
  if (event.altKey) {
    return false
  }

  if (event.key.toLowerCase() !== chord[1].toLowerCase()) {
    return false
  }

  return chord[0] === APPLE_MODIFIER
    ? event.metaKey && !event.ctrlKey
    : event.ctrlKey && !event.metaKey
}

/**
 * How ARIA spells the same chord, for `aria-keyshortcuts`.
 *
 * The attribute takes key *names* from the UI Events spec -- `Meta`,
 * `Control` -- and not the glyphs a keycap shows, so this cannot be the
 * string in the visible hint even though it is the same shortcut.
 */
export function ariaKeyshortcuts(chord: Chord): string {
  return `${chord[0] === APPLE_MODIFIER ? 'Meta' : 'Control'}+${chord[1]}`
}

/**
 * A bare key press, with no modifier and nothing focused that swallows it.
 *
 * The guard every single-key shortcut has to pass. Two halves, and both are
 * failure modes that are silent when they break: a modifier check, so `/`
 * does not fire on Ctrl+/ and steal the browser's binding, and the typing
 * check, so pressing `c` while writing "critical" into a description does not
 * open a composer.
 */
export function isPlainKey(event: KeyboardEvent, key: string): boolean {
  if (event.key !== key) {
    return false
  }

  if (event.ctrlKey || event.metaKey || event.altKey) {
    return false
  }

  return !isTypingTarget(event.target)
}
