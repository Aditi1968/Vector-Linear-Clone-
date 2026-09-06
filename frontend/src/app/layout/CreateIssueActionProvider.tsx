import { useCallback, useState } from 'react'
import type { ReactNode } from 'react'

import {
  CreateIssueActionContext,
  RegisterCreateIssueActionContext,
} from './createIssueAction'
import type { CreateIssueHandler, RegisterCreateIssueAction } from './createIssueAction'

/**
 * Holds the create-issue slot described in ./createIssueAction.ts.
 *
 * Rendered by `AppLayout` above the `<Outlet />`, so it wraps both the
 * sidebar that reads the slot and the screen that fills it.
 *
 * In its own module because it is the only *component* in that seam: keeping
 * it out of `createIssueAction.ts` leaves that file exporting hooks and
 * contexts only, which is what lets Vite's Fast Refresh update a screen
 * without tearing down the provider and losing the registration.
 */
export function CreateIssueActionProvider({ children }: { children: ReactNode }) {
  const [handler, setHandler] = useState<CreateIssueHandler | null>(null)

  const register = useCallback<RegisterCreateIssueAction>((next) => {
    // `setHandler(next)` would be wrong: React reads a function argument as
    // an updater and would call it, storing whatever the handler returned.
    // The extra arrow is what stores the function itself.
    setHandler(() => next)

    return () => {
      // Identity-checked. If a newer screen has already claimed the slot,
      // this cleanup belongs to a screen that has since been replaced, and
      // clearing unconditionally would blank a live registration -- which is
      // exactly the ordering React 19 StrictMode produces on every mount.
      setHandler((current) => (current === next ? null : current))
    }
  }, [])

  return (
    <RegisterCreateIssueActionContext.Provider value={register}>
      <CreateIssueActionContext.Provider value={handler}>
        {children}
      </CreateIssueActionContext.Provider>
    </RegisterCreateIssueActionContext.Provider>
  )
}
