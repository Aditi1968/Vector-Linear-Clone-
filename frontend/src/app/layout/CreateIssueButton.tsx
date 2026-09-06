import { Button, IconButton, PlusIcon } from '../../components'
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
 *
 * Collapsed, it becomes an `IconButton` rather than a `Button` with its label
 * clipped by CSS. The rest of the rail hides labels that way and keeps the
 * accessible name in the text; a primary button cannot, because its padding
 * and minimum width are sized for text and an 8px-wide filled rectangle is
 * not a button anyone can hit. `IconButton` requires an `aria-label`, so the
 * name survives the swap by construction.
 */
export function CreateIssueButton({ collapsed = false }: { collapsed?: boolean }) {
  const createIssue = useCreateIssueAction()

  const shared = {
    variant: 'primary',
    size: 'md',
    disabled: createIssue === null,
    onClick: createIssue ?? undefined,
  } as const

  if (collapsed) {
    return <IconButton {...shared} icon={<PlusIcon />} aria-label="New issue" />
  }

  return (
    <Button {...shared} fullWidth icon={<PlusIcon />}>
      New issue
    </Button>
  )
}
