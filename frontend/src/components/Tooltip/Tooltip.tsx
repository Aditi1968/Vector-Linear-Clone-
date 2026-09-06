import { cloneElement, useId, useState } from 'react'
import type { KeyboardEvent, ReactElement } from 'react'

import { cx } from '../cx'
import styles from './Tooltip.module.css'

/** What `cloneElement` needs to be allowed to write onto the trigger. */
interface Describable {
  'aria-describedby'?: string
}

export interface TooltipProps {
  /** The hint. Keep it short -- this is a reminder, not documentation. */
  label: string
  /** Show below the trigger instead of above, for a trigger near the top edge. */
  placement?: 'top' | 'bottom'
  /**
   * Exactly one element, and it must forward props to a DOM node -- the
   * `aria-describedby` that ties the hint to the control is written onto it.
   *
   * `ReactNode` would be friendlier and would be wrong: the association has to
   * land on the focusable element itself, and a wrapper `<span>` around it does
   * not describe the button inside. Making the requirement a type error is
   * better than shipping a tooltip nobody hears.
   */
  children: ReactElement<Describable>
  className?: string
}

/**
 * A hint attached to a control.
 *
 * Opens on hover *and* on keyboard focus, which is the half people forget: a
 * hover-only tooltip is invisible to anyone driving the app from the keyboard,
 * and icon-only toolbars are exactly where tooltips get used.
 *
 * The hint is `role="tooltip"` and is described by, not labelled by. A tooltip
 * must never be a control's only name -- that is what `aria-label` on the
 * button is for -- because a tooltip is not reachable on touch devices at all.
 *
 * Mounted only while open, so the text is absent from the accessibility tree
 * the rest of the time; a permanently rendered, visually hidden copy would be
 * read out on every pass through the page.
 */
export function Tooltip({
  label,
  placement = 'top',
  children,
  className,
}: TooltipProps) {
  const id = useId()
  const [open, setOpen] = useState(false)

  /* Escape closes it. Required by the WCAG "content on hover or focus"
   * criterion: a tooltip that covers something must be dismissible without
   * moving the pointer, since a magnifier user may not be able to move away
   * from it without losing their place. */
  const handleKeyDown = (event: KeyboardEvent<HTMLSpanElement>) => {
    if (event.key === 'Escape' && open) {
      setOpen(false)
    }
  }

  /* Merged, not replaced: a trigger may already be described by an error
   * message, and clobbering that reference would silence it. */
  const describedBy = open
    ? [children.props['aria-describedby'], id].filter(Boolean).join(' ')
    : children.props['aria-describedby']

  return (
    <span
      className={cx(styles.root, className)}
      onPointerEnter={() => {
        setOpen(true)
      }}
      onPointerLeave={() => {
        setOpen(false)
      }}
      /* `onFocus`/`onBlur` rather than the non-bubbling native focus events:
       * React's versions bubble, so the wrapper sees focus land on whatever
       * control the caller passed as `children` without needing a ref to it. */
      onFocus={() => {
        setOpen(true)
      }}
      onBlur={() => {
        setOpen(false)
      }}
      onKeyDown={handleKeyDown}
    >
      {cloneElement(children, { 'aria-describedby': describedBy })}
      {open && (
        <span
          role="tooltip"
          id={id}
          className={cx(styles.tooltip, placement === 'bottom' && styles.below)}
        >
          {label}
        </span>
      )}
    </span>
  )
}
