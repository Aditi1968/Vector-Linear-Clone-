import { createContext, use, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

import { IconButton } from '../Button'
import { AlertIcon, CheckIcon, CloseIcon } from '../icons'
import { cx } from '../cx'
import styles from './Toast.module.css'

export type ToastTone = 'neutral' | 'success' | 'danger'

export interface ToastOptions {
  title: string
  description?: string
  tone?: ToastTone
  /** Milliseconds before it disappears. `0` keeps it until dismissed. */
  duration?: number
}

interface ActiveToast extends ToastOptions {
  id: string
}

interface ToastApi {
  /** Show a notification. Returns its id so a long-running one can be dismissed. */
  toast: (options: ToastOptions) => string
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

/**
 * Read the toast API.
 *
 * Throws rather than returning `null` when the provider is missing. A silently
 * no-op `toast()` is worse than a crash: the failure shows up as "the app
 * never told me the save worked", weeks later, with nothing in the console.
 */
export function useToast(): ToastApi {
  const api = use(ToastContext)
  if (api === null) {
    throw new Error('useToast must be used inside a <ToastProvider>')
  }
  return api
}

/** How long a toast stays if the caller does not say. */
const DEFAULT_DURATION = 6000

export interface ToastProviderProps {
  children: ReactNode
}

/**
 * Holds the queue and renders the region.
 *
 * Mount once, near the root. The provider owns both halves on purpose --
 * splitting the state from the region would let an app render the provider and
 * forget the region, and the resulting silence looks exactly like "no
 * notifications happened".
 */
export function ToastProvider({ children }: ToastProviderProps) {
  const [toasts, setToasts] = useState<readonly ActiveToast[]>([])
  const nextId = useRef(0)

  const dismiss = useCallback((id: string) => {
    setToasts((current) => current.filter((item) => item.id !== id))
  }, [])

  const toast = useCallback((options: ToastOptions) => {
    nextId.current += 1
    const id = `toast-${String(nextId.current)}`
    setToasts((current) => [...current, { ...options, id }])
    return id
  }, [])

  const api = useMemo(() => ({ toast, dismiss }), [toast, dismiss])

  return (
    <ToastContext value={api}>
      {children}
      <ToastRegion toasts={toasts} onDismiss={dismiss} />
    </ToastContext>
  )
}

interface ToastRegionProps {
  toasts: readonly ActiveToast[]
  onDismiss: (id: string) => void
}

function ToastRegion({ toasts, onDismiss }: ToastRegionProps) {
  return (
    /**
     * `aria-live="polite"` on the list, not on each toast.
     *
     * The live region has to be the element that is already on the page; the
     * announcement is triggered by children being *added to* it. `polite` waits
     * for the reader to finish its sentence -- an error toast uses `role="alert"`
     * on the item itself when it needs to cut in.
     */
    <ol
      className={styles.region}
      aria-live="polite"
      aria-label="Notifications"
      /* `additions` only: a toast being removed is not news, and announcing
       * removals means every auto-dismiss is read out a second time. */
      aria-relevant="additions"
    >
      {toasts.map((item) => (
        <ToastItem key={item.id} toast={item} onDismiss={onDismiss} />
      ))}
    </ol>
  )
}

interface ToastItemProps {
  toast: ActiveToast
  onDismiss: (id: string) => void
}

function ToastItem({ toast, onDismiss }: ToastItemProps) {
  const { id, title, description, tone = 'neutral', duration = DEFAULT_DURATION } = toast

  useEffect(() => {
    if (duration <= 0) return
    const timer = setTimeout(() => {
      onDismiss(id)
    }, duration)
    return () => {
      clearTimeout(timer)
    }
  }, [id, duration, onDismiss])

  return (
    <li
      className={cx(styles.toast, tone !== 'neutral' && styles[tone])}
      /* Failures interrupt; confirmations wait their turn. */
      role={tone === 'danger' ? 'alert' : undefined}
    >
      {tone !== 'neutral' && (
        <span className={styles.glyph}>
          {tone === 'danger' ? <AlertIcon /> : <CheckIcon />}
        </span>
      )}
      <div className={styles.body}>
        <div className={styles.title}>{title}</div>
        {description !== undefined && (
          <div className={styles.description}>{description}</div>
        )}
      </div>
      <IconButton
        size="sm"
        icon={<CloseIcon />}
        aria-label={`Dismiss: ${title}`}
        className={styles.dismiss}
        onClick={() => {
          onDismiss(id)
        }}
      />
    </li>
  )
}
