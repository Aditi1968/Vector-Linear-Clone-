import { createContext, useCallback, useContext, useEffect, useRef } from 'react'

/**
 * The seam between the shell's create-issue affordance and the issue
 * composer.
 *
 * The problem it solves: the sidebar owns a "New issue" button, but the
 * composer that button opens is the issues feature's, and the shell must not
 * know anything about it. The obvious alternatives are both worse.
 *
 *   - The shell imports the composer. That inverts the dependency -- generic
 *     chrome now depends on one specific feature -- and it means the shell
 *     cannot render at all until the composer exists.
 *   - The shell navigates to a `/issues/new` route. That commits the product
 *     to a routed composer, and the composer is not this teammate's design
 *     decision to make.
 *
 * So instead: the shell publishes a *slot*, and whichever screen is mounted
 * fills it. The screen that owns issue creation calls
 * `useRegisterCreateIssueAction(handler)`; the sidebar reads the slot with
 * `useCreateIssueAction()`.
 *
 * The empty slot is meaningful, not a gap. When nothing has registered --
 * on the not-found page, say -- `useCreateIssueAction()` returns `null` and
 * the sidebar renders the button *disabled*. That is the honest state: there
 * is nothing here that can create an issue. A button that looked live and
 * did nothing would be exactly the kind of dishonest UI this phase is trying
 * to avoid.
 *
 * One slot, not a stack. Exactly one screen is mounted at a time under this
 * router, so a second registration replacing the first is correct rather than
 * a conflict. Unregistering is identity-checked so that a mount/unmount
 * *interleaving* -- React 19 StrictMode's double-invoked effects, or a route
 * transition where the incoming screen registers before the outgoing one
 * cleans up -- cannot leave the slot empty when a live screen still owns it.
 */
export type CreateIssueHandler = () => void

/**
 * Claim the slot. Returns the cleanup that releases it, so the shape matches
 * what `useEffect` expects to be handed back.
 */
export type RegisterCreateIssueAction = (handler: CreateIssueHandler) => () => void

/**
 * The currently registered handler, or `null` when the mounted screen cannot
 * create issues.
 *
 * Split from the register context on purpose: `register` is referentially
 * stable for the life of the provider, while this value changes on every
 * registration. Keeping them apart means the sidebar re-renders when the
 * handler changes and the *screens* -- which only ever read `register` --
 * do not.
 */
export const CreateIssueActionContext = createContext<CreateIssueHandler | null>(null)

export const RegisterCreateIssueActionContext =
  createContext<RegisterCreateIssueAction | null>(null)

/**
 * Read the create-issue action. `null` means no mounted screen offers one.
 *
 * For the shell. Feature code should not need this.
 */
export function useCreateIssueAction(): CreateIssueHandler | null {
  return useContext(CreateIssueActionContext)
}

/**
 * Offer this screen's issue composer to the shell's create affordance.
 *
 * Call it unconditionally from a screen that can create issues:
 *
 *     useRegisterCreateIssueAction(openComposer)
 *
 * `handler` may be a fresh closure on every render -- that is the normal case
 * for something built from `useState` setters and Apollo mutations, and
 * requiring the caller to `useCallback` it correctly would make a subtle
 * dependency-array mistake into a silently broken button. The handler is held
 * in a ref and invoked through a stable wrapper, so the registration effect
 * runs once per mount no matter how the caller writes the callback, while the
 * button still calls the *latest* closure and never a stale one.
 *
 * Outside a `CreateIssueActionProvider` this is a no-op rather than a throw:
 * a screen must remain mountable in a test or a Storybook-style harness
 * without dragging the whole shell in, and a screen is not broken by the
 * absence of chrome it does not own.
 */
export function useRegisterCreateIssueAction(handler: CreateIssueHandler): void {
  const register = useContext(RegisterCreateIssueActionContext)

  const latestHandler = useRef(handler)
  useEffect(() => {
    latestHandler.current = handler
  })

  const stableHandler = useCallback<CreateIssueHandler>(() => {
    latestHandler.current()
  }, [])

  useEffect(() => {
    if (register === null) {
      return undefined
    }
    return register(stableHandler)
  }, [register, stableHandler])
}
