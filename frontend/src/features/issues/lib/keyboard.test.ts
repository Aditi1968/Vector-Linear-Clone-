import { describe, expect, it } from 'vitest'

import { isTypingTarget } from './keyboard'

/**
 * The guard that makes a single-letter shortcut safe.
 *
 * Unit tests, because the integrated proof in ../keyboard.test.tsx can only
 * reach this through elements the application actually renders, and the set
 * of elements this function has to get right is larger than that. Get it
 * wrong and typing the word "critical" into a description opens a composer --
 * a bug invisible to every test that does not type the letter c.
 */
describe('isTypingTarget', () => {
  it.each(['input', 'textarea', 'select'] as const)('is true for <%s>', (tagName) => {
    expect(isTypingTarget(document.createElement(tagName))).toBe(true)
  })

  it('is true for a contenteditable element of any tag', () => {
    const editor = document.createElement('div')

    /*
      jsdom does not implement `isContentEditable` -- setting the attribute or
      the property leaves it `undefined` -- so the property is defined here to
      stand in for the browser behaviour this branch exists to handle.

      This shims the *environment*, not the code under test: `isTypingTarget`
      runs unmodified and the assertion is about what it returns. Without it
      the contenteditable branch could not be reached in jsdom at all, and an
      editable `<div>` is exactly the case that gets missed -- its tag name is
      on nobody's list.
    */
    Object.defineProperty(editor, 'isContentEditable', { value: true })

    expect(isTypingTarget(editor)).toBe(true)
  })

  it.each(['div', 'button', 'a', 'span'] as const)('is false for <%s>', (tagName) => {
    expect(isTypingTarget(document.createElement(tagName))).toBe(false)
  })

  it('is false for a non-element target', () => {
    // `event.target` is `EventTarget | null`, which includes `window` and
    // `document` -- neither is an element and neither is being typed into.
    expect(isTypingTarget(null)).toBe(false)
    expect(isTypingTarget(window)).toBe(false)
    expect(isTypingTarget(document)).toBe(false)
  })
})
