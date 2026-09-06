import { Button, PlusIcon } from '../../components'
import { useCreateIssueAction } from './createIssueAction'

/**
 * The shell's create affordance.
 *
 * It knows nothing about issue creation. It reads the slot published by
 * ./createIssueAction.ts and calls whatever the mounted screen registered
 * there; the composer belongs to the issues feature.
 *
 * When no screen has registered, the button is `disabled` rather than hidden.
 * Hiding it would make the sidebar's shape change between routes, which is
 * disorienting in persistent chrome; disabling it is also the truthful state,
 * since on a screen with no composer there genuinely is nothing to open. What
 * it must never be is enabled-and-inert -- a primary button that swallows a
 * click is a bug report, not a feature gap.
 *
 * `aria-disabled` is not used in place of `disabled`. The pattern of keeping
 * an unusable control focusable is for controls that fail *conditionally* and
 * need to explain why; here there is nothing to explain beyond "not on this
 * screen", and leaving a dead primary action in the tab order on every page
 * is worse than leaving it out.
 */
export function CreateIssueButton() {
  const createIssue = useCreateIssueAction()

  return (
    <Button
      variant="primary"
      size="md"
      fullWidth
      icon={<PlusIcon />}
      disabled={createIssue === null}
      onClick={createIssue ?? undefined}
    >
      New issue
    </Button>
  )
}
