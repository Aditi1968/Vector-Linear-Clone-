import { useEffect, useId, useRef } from 'react'
import type { MouseEvent, ReactNode } from 'react'

import { IconButton } from '../Button'
import { CloseIcon } from '../icons'
import { cx } from '../cx'
import styles from './Dialog.module.css'

export type DialogSize = 'sm' | 'md' | 'lg'

export interface DialogProps {
  open: boolean
  /** Called for every close path: the button, Escape, and a click on the scrim. */
  onClose: () => void
  /** The dialog's accessible name. Rendered as the heading. */
  title: string
  /** One line under the title, associated as the accessible description. */
  description?: string
  children: ReactNode
  /** Actions. Confirm last, so it sits nearest the cursor's resting place. */
  footer?: ReactNode
  size?: DialogSize
  className?: string
}

/**
 * A modal dialog, built on the platform's `<dialog>`.
 *
 * `showModal()` is the whole reason this file is short. The browser gives us,
 * for free and correctly:
 *
 *   - a focus trap (everything outside is made inert)
 *   - initial focus inside the dialog
 *   - focus *returned* to whatever was focused before, on close
 *   - Escape to dismiss, via the `cancel` event
 *   - the top layer, so no z-index can ever paint over it
 *   - `::backdrop`, a scrim that needs no extra element
 *   - `aria-modal` semantics without the attribute
 *
 * Every one of those is a defect waiting to happen in a hand-rolled overlay,
 * and the focus-return in particular is the one that gets skipped. Reaching
 * for a dialog library here would be paying a dependency to reimplement
 * something the browser already does better.
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  className,
}: DialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const descriptionId = useId()

  /**
   * Drive the element's own open state from the prop.
   *
   * The `open` *attribute* is deliberately not used: setting it shows a
   * non-modal dialog, with no backdrop, no focus trap and no top layer.
   * `showModal()` is the only way to get the modal behaviour, and it is a
   * method, so it has to happen in an effect.
   */
  useEffect(() => {
    const dialog = dialogRef.current
    if (dialog === null) return

    /**
     * Feature-detected, because jsdom implements `HTMLDialogElement` without
     * `showModal()` -- so an unguarded call crashes every test that renders a
     * dialog, including tests in features that merely happen to open one.
     * The `open` attribute is the degraded path: the content is rendered and
     * queryable, without the top layer or the focus trap that only the real
     * method can provide. No browser takes this branch.
     */
    if (open && !dialog.open) {
      if (typeof dialog.showModal === 'function') {
        dialog.showModal()
      } else {
        dialog.setAttribute('open', '')
      }
    } else if (!open && dialog.open) {
      if (typeof dialog.close === 'function') {
        dialog.close()
      } else {
        dialog.removeAttribute('open')
      }
    }
  }, [open])

  /**
   * Tell the caller when the browser closed the dialog behind our back.
   *
   * Escape dismisses a modal dialog natively, so without this the element
   * would be closed while `open` was still `true` -- and the next click on the
   * trigger would set a prop that had not changed, leaving the dialog shut.
   */
  useEffect(() => {
    const dialog = dialogRef.current
    if (dialog === null) return

    const handleClose = () => {
      onClose()
    }

    dialog.addEventListener('close', handleClose)
    return () => {
      dialog.removeEventListener('close', handleClose)
    }
  }, [onClose])

  /**
   * Click on the scrim closes.
   *
   * The backdrop is not an element that can take a listener, but it is painted
   * *behind* the dialog box and clicks on it are retargeted to the `<dialog>`
   * itself -- so a click whose target is the dialog element, rather than
   * anything inside `.content`, happened outside the visible panel.
   */
  const handleClick = (event: MouseEvent<HTMLDialogElement>) => {
    if (event.target === dialogRef.current) {
      onClose()
    }
  }

  return (
    <dialog
      ref={dialogRef}
      className={cx(styles.dialog, styles[size], className)}
      aria-labelledby={titleId}
      aria-describedby={description === undefined ? undefined : descriptionId}
      onClick={handleClick}
    >
      {/* Mounted only while open, so a form inside starts empty every time
        * rather than showing whatever the user typed before cancelling. */}
      {open && (
        <div className={styles.content}>
          <div className={styles.header}>
            <div className={styles.heading}>
              <h2 id={titleId}>{title}</h2>
              {description !== undefined && (
                <p id={descriptionId} className={styles.description}>
                  {description}
                </p>
              )}
            </div>
            <IconButton
              icon={<CloseIcon />}
              aria-label="Close dialog"
              onClick={onClose}
            />
          </div>

          <div className={styles.body}>{children}</div>

          {footer !== undefined && <div className={styles.footer}>{footer}</div>}
        </div>
      )}
    </dialog>
  )
}
