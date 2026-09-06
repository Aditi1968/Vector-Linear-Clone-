/**
 * Deciding whether a keystroke belongs to something the user is typing into.
 *
 * A single-letter shortcut like `C` is only safe if it never fires while a
 * field has focus. Get this wrong and typing the word "create" into a
 * description opens a second composer -- a bug that is invisible in every
 * test that does not type the letter c.
 *
 * `contentEditable` is checked first and separately: an editable `<div>` has
 * a tag name that is on nobody's list, and it is the case that gets missed.
 * The tag list is deliberately short -- input, textarea, select -- because
 * those are the elements this application actually renders. `<select>` is on
 * it because typing a letter in a native select jumps to a matching option,
 * which is real text entry even though nothing looks like a text box.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false
  }

  if (target.isContentEditable) {
    return true
  }

  const tagName = target.tagName

  return tagName === 'INPUT' || tagName === 'TEXTAREA' || tagName === 'SELECT'
}
