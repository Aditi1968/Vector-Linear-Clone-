import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import {
  CreateIssueActionProvider,
  useCreateIssueAction,
  useRegisterCreateIssueAction,
} from './index'

/**
 * The create-issue slot, and the ordering its identity check exists for.
 *
 * ================================================================
 * Why this is not covered by the StrictMode test in ../shell.test.tsx
 * ================================================================
 *
 * `CreateIssueActionProvider`'s unregister is identity-checked --
 * `setHandler((current) => (current === next ? null : current))` -- rather
 * than clearing unconditionally. The obvious way to test that is to mount the
 * shell under `<StrictMode>` and check the button still works, and it does
 * not test it at all: StrictMode's order is register -> cleanup -> register,
 * so the *last* thing to run is a registration and the final state is correct
 * whether or not the cleanup checked identity.
 *
 * The ordering the check actually guards is the other one -- register A,
 * register B, then A's cleanup -- where an unconditional clear would blank a
 * registration a live screen still owns. That cannot be produced through the
 * real route table, because `IssueListPage` is the only screen that registers
 * and React runs an outgoing component's cleanup before an incoming one's
 * effects. So it is produced here directly, with two registrars and a
 * controlled unmount.
 */

/** Stands in for a screen that can create issues. */
function Registrar({ onCreate }: { onCreate: () => void }) {
  useRegisterCreateIssueAction(onCreate)

  return null
}

/** Stands in for the sidebar's button, which is all that reads the slot. */
function SlotProbe() {
  const createIssue = useCreateIssueAction()

  return (
    <button
      type="button"
      disabled={createIssue === null}
      onClick={createIssue ?? undefined}
    >
      Create from the shell
    </button>
  )
}

function slotButton(): HTMLElement {
  return screen.getByRole('button', { name: 'Create from the shell' })
}

describe('the create-issue slot', () => {
  it('keeps a live registration when a replaced screen cleans up afterwards', async () => {
    const user = userEvent.setup()
    const outgoing = vi.fn()
    const incoming = vi.fn()

    /*
      Keyed, and that is load-bearing. Without keys React reconciles these by
      position, so removing the first element would keep its instance mounted
      with the second's props and unmount the second -- the exact opposite of
      the ordering under test, and a test that would pass for the wrong
      reason.
    */
    const view = render(
      <CreateIssueActionProvider>
        <SlotProbe />
        <Registrar key="outgoing" onCreate={outgoing} />
      </CreateIssueActionProvider>,
    )

    await user.click(slotButton())
    expect(outgoing).toHaveBeenCalledTimes(1)

    // A second screen claims the slot while the first is still mounted.
    view.rerender(
      <CreateIssueActionProvider>
        <SlotProbe />
        <Registrar key="outgoing" onCreate={outgoing} />
        <Registrar key="incoming" onCreate={incoming} />
      </CreateIssueActionProvider>,
    )

    await user.click(slotButton())
    expect(incoming).toHaveBeenCalledTimes(1)
    expect(outgoing).toHaveBeenCalledTimes(1)

    // Now the replaced screen unmounts, so its cleanup runs *after* the
    // newer registration.
    view.rerender(
      <CreateIssueActionProvider>
        <SlotProbe />
        <Registrar key="incoming" onCreate={incoming} />
      </CreateIssueActionProvider>,
    )

    // Unconditional cleanup would have blanked the slot here, and the
    // sidebar's primary action would arrive disabled on a screen that has a
    // composer.
    expect(slotButton()).toBeEnabled()

    await user.click(slotButton())
    expect(incoming).toHaveBeenCalledTimes(2)
    expect(outgoing).toHaveBeenCalledTimes(1)

    // And cleanup does run and does clear when it owns the registration --
    // without this the test above would also pass against a cleanup that
    // never fired at all.
    view.rerender(
      <CreateIssueActionProvider>
        <SlotProbe />
      </CreateIssueActionProvider>,
    )

    expect(slotButton()).toBeDisabled()
  })

  it('is a no-op outside a provider rather than a throw', () => {
    const onCreate = vi.fn()

    // A screen must stay mountable on its own -- which is what lets every
    // issues test render a page without dragging the shell in -- and a screen
    // is not broken by the absence of chrome it does not own.
    expect(() => {
      render(<Registrar onCreate={onCreate} />)
    }).not.toThrow()

    expect(onCreate).not.toHaveBeenCalled()
  })
})
