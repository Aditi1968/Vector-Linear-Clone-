import type { FocusEvent, KeyboardEvent } from 'react'

import { cx } from '../../../components'
import styles from '../issues.module.css'

export interface EditableTextProps {
  /** Ties this control to its `<label>`. */
  id: string
  /**
   * The ARIA wiring, spelled as the attributes rather than as `invalid` and
   * `describedBy`.
   *
   * So that `<EditableText {...control} />` and `<Select {...control} />`
   * take the same object from `IssueInspector`'s `Property`: a second
   * spelling here would mean every call site translating between the two,
   * which is exactly where an `aria-describedby` goes missing.
   */
  'aria-describedby'?: string
  'aria-invalid'?: true
  /** The server's current value. An empty string for "nothing stored". */
  value: string
  /** Called only when the value actually changed. */
  onCommit: (next: string) => void
  placeholder?: string
  /**
   * The native input type, for the two fields that are not free text.
   *
   * `number` and `date` rather than a parser and a picker: the browser
   * already validates the shape, offers the right keyboard on a phone, and
   * -- for `date` -- renders a calendar this application does not have to
   * ship, style or make accessible. The value still arrives here as a
   * string, and `date` hands back exactly the `YYYY-MM-DD` the `Date` scalar
   * wants.
   */
  type?: 'text' | 'number' | 'date'
  /** Render a `<textarea>` instead of an `<input>`. */
  multiline?: boolean
  className?: string
  disabled?: boolean
}

/**
 * A text field that saves itself.
 *
 * ## Why commit on blur rather than on a Save button
 *
 * This is a dense product, and a title with a Save button beside it is two
 * clicks and a mode. Blur is the moment the user has demonstrably finished
 * with the field, Escape is the documented way out without saving, and Enter
 * on a single-line field means "done" -- which is what a keyboard user
 * reaches for and what a button would not give them without a tab stop.
 *
 * ## Why it is uncontrolled
 *
 * The obvious controlled version needs a draft in state plus an effect to
 * resync it when the server's value changes underneath, and that effect is
 * where the bug lives: it either clobbers what the user is typing or fails
 * to pick up a change made elsewhere. An uncontrolled field keyed on the
 * server value has neither problem -- React remounts the element exactly
 * when the stored value changes, which after a commit of our own is a field
 * that has already been blurred, and after someone else's change is a field
 * that should show theirs.
 *
 * The trade is that a value changed by someone else *while this field has
 * focus* takes the draft with it. That is the correct outcome far more often
 * than not, and the alternative is the effect above.
 *
 * ## No commit when nothing changed
 *
 * Tabbing through a form must not send eight mutations. The comparison is
 * against the server's value, not against a "dirty" flag, so typing a
 * character and deleting it again is also silent.
 */
export function EditableText({
  id,
  value,
  onCommit,
  placeholder,
  type = 'text',
  multiline = false,
  className,
  disabled = false,
  'aria-describedby': describedBy,
  'aria-invalid': invalid,
}: EditableTextProps) {
  const commit = (event: FocusEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    if (event.currentTarget.value !== value) {
      onCommit(event.currentTarget.value)
    }
  }

  const handleKeyDown = (
    event: KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>,
  ) => {
    if (event.key === 'Escape') {
      // Revert and leave. Restoring the DOM value before blurring is what
      // makes the blur handler above see "unchanged" and send nothing.
      event.currentTarget.value = value
      event.currentTarget.blur()
      return
    }

    // Enter in a textarea is a newline; only a single-line field treats it as
    // "finished". Blur rather than a direct commit, so there is one code path
    // that saves and the field really does lose focus.
    if (event.key === 'Enter' && !multiline) {
      event.preventDefault()
      event.currentTarget.blur()
    }
  }

  const shared = {
    // The server's value is the key, so an external change remounts the
    // element with the new text and a change of our own does not fight the
    // user's cursor. See the note above.
    key: value,
    id,
    defaultValue: value,
    placeholder,
    disabled,
    'aria-invalid': invalid,
    'aria-describedby': describedBy,
    onBlur: commit,
    onKeyDown: handleKeyDown,
  }

  return multiline ? (
    <textarea
      {...shared}
      className={cx(styles.editable, styles.editableBody, className)}
      rows={5}
    />
  ) : (
    <input {...shared} className={cx(styles.editable, className)} type={type} />
  )
}
