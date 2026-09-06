import { Fragment, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'

import { Button } from '../Button'
import type { ButtonSize, ButtonVariant } from '../Button'
import { MoreIcon } from '../icons'
import { cx } from '../cx'
import styles from './Menu.module.css'

export interface MenuItem {
  id: string
  label: string
  /** Decorative leading glyph. */
  icon?: ReactNode
  /** A keyboard hint, shown right-aligned. Display only -- binding it is the caller's job. */
  shortcut?: string
  /** Deletes something, or is otherwise hard to undo. Rendered in the danger colour. */
  destructive?: boolean
  disabled?: boolean
  onSelect: () => void
  /** Draw a divider above this item. */
  separatorBefore?: boolean
}

export interface MenuProps {
  /** The trigger's accessible name -- "More actions on VEC-12". */
  label: string
  items: readonly MenuItem[]
  /** Trigger glyph. Defaults to the overflow dots. */
  icon?: ReactNode
  /** Visible trigger text. Without it the trigger is icon-only and named by `label`. */
  children?: ReactNode
  variant?: ButtonVariant
  size?: ButtonSize
  /** Which edge of the trigger the panel hangs from. */
  align?: 'start' | 'end'
  className?: string
}

/**
 * A dropdown menu of actions.
 *
 * Built rather than borrowed, and built out of buttons. Every item is a real
 * `<button>`, so Enter and Space activate it, it is announced with a state,
 * and `disabled` actually disables it -- none of which a `<div onClick>` with
 * `role="menuitem"` gets without reimplementation.
 *
 * Keyboard contract (WAI-ARIA menu button):
 *   ArrowDown / ArrowUp on the trigger  open and focus the first / last item
 *   ArrowDown / ArrowUp in the menu     move, wrapping at both ends
 *   Home / End                          first / last item
 *   Escape                              close and return focus to the trigger
 *   Tab                                 close, and let focus move on normally
 *
 * There is no focus trap. A menu is not modal -- tabbing out of one is a
 * legitimate way to leave it, and trapping focus in a transient panel is how
 * keyboard users get stuck. Focus *return* is what matters, and it happens on
 * every close path: escape, selection, and clicking away.
 */
export function Menu({
  label,
  items,
  icon,
  children,
  variant = 'ghost',
  size = 'sm',
  align = 'start',
  className,
}: MenuProps) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  /* Which end to land on when the panel opens. A ref rather than state because
   * it must be readable by the open effect without causing a second render,
   * and it is never rendered itself. */
  const openEdgeRef = useRef<'first' | 'last'>('first')

  const close = (returnFocus: boolean) => {
    setOpen(false)
    if (returnFocus) {
      triggerRef.current?.focus()
    }
  }

  /**
   * Enabled items, read from the DOM rather than tracked in state.
   *
   * The DOM is already the list -- it knows the render order and which items
   * are disabled -- and a parallel ref array would be a second copy that has to
   * be kept in step with it on every render.
   */
  const enabledItems = () =>
    Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]:not(:disabled)',
      ) ?? [],
    )

  const moveFocus = (delta: number) => {
    const nodes = enabledItems()
    if (nodes.length === 0) return
    const current = nodes.findIndex((node) => node === document.activeElement)
    // Wrapping modulo: -1 (nothing focused yet) with delta 1 lands on 0.
    const next = (current + delta + nodes.length) % nodes.length
    nodes[next]?.focus()
  }

  const focusEdge = (edge: 'first' | 'last') => {
    const nodes = enabledItems()
    const node = edge === 'first' ? nodes.at(0) : nodes.at(-1)
    node?.focus()
  }

  // Opening moves focus into the panel, which is what makes the menu usable
  // from the keyboard at all. An effect rather than a call in the handler
  // because the items do not exist until after the open render commits.
  useEffect(() => {
    if (open) {
      focusEdge(openEdgeRef.current)
    }
  }, [open])

  /**
   * Close when a press lands outside.
   *
   * `pointerdown`, not `click`: a click fires after mouseup, so a press that
   * starts outside and drags back in would not close the menu -- and, worse,
   * pressing a control outside would both close the menu and activate that
   * control, which is right, but only if the close happens first.
   */
  useEffect(() => {
    if (!open) return

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target
      if (!(target instanceof Node)) return
      // The trigger is excluded so its own click still toggles rather than
      // being closed here and reopened by the click handler.
      if (menuRef.current?.contains(target) === true) return
      if (triggerRef.current?.contains(target) === true) return
      setOpen(false)
    }

    document.addEventListener('pointerdown', onPointerDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
    }
  }, [open])

  const handleTriggerKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      openEdgeRef.current = event.key === 'ArrowUp' ? 'last' : 'first'
      setOpen(true)
    }
  }

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        moveFocus(1)
        break
      case 'ArrowUp':
        event.preventDefault()
        moveFocus(-1)
        break
      case 'Home':
        event.preventDefault()
        focusEdge('first')
        break
      case 'End':
        event.preventDefault()
        focusEdge('last')
        break
      case 'Escape':
        event.preventDefault()
        close(true)
        break
      case 'Tab':
        // Not prevented: the menu closes and focus continues out of it, which
        // is the documented way to leave a non-modal menu.
        setOpen(false)
        break
      default:
        break
    }
  }

  const triggerProps = {
    ref: triggerRef,
    variant,
    size,
    'aria-haspopup': 'menu',
    'aria-expanded': open,
    onClick: () => {
      openEdgeRef.current = 'first'
      setOpen((wasOpen) => !wasOpen)
    },
    onKeyDown: handleTriggerKeyDown,
  } as const

  return (
    <div className={cx(styles.root, className)}>
      {children === undefined ? (
        <Button {...triggerProps} icon={icon ?? <MoreIcon />} aria-label={label} />
      ) : (
        <Button {...triggerProps} icon={icon}>
          {children}
        </Button>
      )}

      {open && (
        <div
          ref={menuRef}
          role="menu"
          aria-label={label}
          className={cx(styles.menu, align === 'end' && styles.alignEnd)}
          onKeyDown={handleMenuKeyDown}
        >
          {/* Fragment, not a wrapper element: `role="menu"` requires its owned
            * items to be its direct children, and an intervening `<div>` breaks
            * that relationship in the accessibility tree. */}
          {items.map((item) => (
            <Fragment key={item.id}>
              {item.separatorBefore === true && (
                <div role="separator" className={styles.separator} />
              )}
              <button
                type="button"
                role="menuitem"
                disabled={item.disabled}
                className={cx(
                  styles.item,
                  item.destructive === true && styles.destructive,
                )}
                onClick={() => {
                  item.onSelect()
                  close(true)
                }}
              >
                {item.icon !== undefined && (
                  <span className={styles.itemIcon}>{item.icon}</span>
                )}
                <span className={styles.itemLabel}>{item.label}</span>
                {item.shortcut !== undefined && (
                  <span className={styles.shortcut} aria-hidden="true">
                    {item.shortcut}
                  </span>
                )}
              </button>
            </Fragment>
          ))}
        </div>
      )}
    </div>
  )
}
