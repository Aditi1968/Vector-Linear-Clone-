import { useId, useState } from 'react'

import {
  Button,
  EmptyState,
  Input,
  LabelIcon,
  Menu,
  PlusIcon,
  Tag,
} from '../../../components'
import type { MenuItem } from '../../../components'
import { describeOutcome, useLabels } from '../api'
import styles from '../collaboration.module.css'
import { Panel } from './Panel'

export interface LabelsPanelProps {
  workspaceSlug: string
  issueId: string
}

/**
 * The labels on an issue, and the workspace list they come from.
 *
 * The picker is a `Menu` rather than a dialog: attaching a label is a small,
 * reversible, frequently-repeated action, and a modal for it would be four
 * interactions where one will do. Detaching is the `Tag`'s own remove button,
 * which is a real `<button>` inside the tag rather than the tag itself being
 * clickable -- clicking a label usually means "filter by this", and one
 * target cannot mean both.
 *
 * Creating a label inline is two mutations and it says so: there is no
 * create-and-attach operation in the schema, so a create that succeeds and an
 * attach that fails leaves a real label in the workspace. The message says
 * which half happened rather than implying a rollback that did not occur.
 */
export function LabelsPanel({ workspaceSlug, issueId }: LabelsPanelProps) {
  const {
    labels,
    attachable,
    isLoading,
    errorMessage,
    retry,
    hasMoreLabels,
    isLoadingMoreLabels,
    loadMoreLabels,
    attach,
    detach,
    createAndAttach,
    isBusy,
  } = useLabels(workspaceSlug, issueId)

  const [status, setStatus] = useState<string | null>(null)
  const [isCreating, setIsCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [formError, setFormError] = useState<string | null>(null)
  const nameId = useId()

  const report = (outcome: Awaited<ReturnType<typeof attach>>, done: string) => {
    setStatus(outcome.status === 'ok' ? done : describeOutcome(outcome))
  }

  const items: MenuItem[] = attachable.map((label) => ({
    id: label.id,
    label: label.name,
    onSelect: () => {
      void attach(label.id).then((outcome) => {
        report(outcome, `${label.name} added`)
      })
    },
  }))

  if (hasMoreLabels) {
    // The picker shows one page of the workspace's labels. Rather than
    // pretending that is all of them, the next page is an item -- selecting it
    // closes the menu, which is what `Menu` does on every selection, so the
    // list is longer the next time it opens.
    items.push({
      id: 'load-more',
      label: isLoadingMoreLabels ? 'Loading more labels' : 'Show more labels',
      disabled: isLoadingMoreLabels,
      separatorBefore: true,
      onSelect: loadMoreLabels,
    })
  }

  items.push({
    id: 'create',
    label: 'New label',
    icon: <PlusIcon />,
    separatorBefore: !hasMoreLabels,
    onSelect: () => {
      setFormError(null)
      setIsCreating(true)
    },
  })

  const submitNewLabel = async () => {
    const name = newName.trim()

    if (name.length === 0) {
      setFormError('Give the label a name.')
      return
    }

    setFormError(null)
    const outcome = await createAndAttach(name)

    if (outcome.status !== 'ok') {
      setFormError(describeOutcome(outcome))
      return
    }

    setNewName('')
    setIsCreating(false)
    setStatus(`${name} created and added`)
  }

  return (
    <Panel
      title="Labels"
      count={isLoading ? undefined : labels.length}
      isLoading={isLoading}
      errorMessage={errorMessage}
      onRetry={retry}
      status={status}
      action={
        <Menu
          label="Add a label"
          items={items}
          icon={<PlusIcon />}
          align="end"
          size="sm"
        >
          Add
        </Menu>
      }
    >
      {labels.length === 0 ? (
        <EmptyState
          icon={<LabelIcon />}
          title="No labels"
          description="Labels group issues across teams and projects."
        />
      ) : (
        <ul className={styles.tags} aria-label="Labels on this issue">
          {labels.map((label) => (
            <li key={label.id}>
              <Tag
                name={label.name}
                color={label.color}
                onRemove={() => {
                  void detach(label.id).then((outcome) => {
                    report(outcome, `${label.name} removed`)
                  })
                }}
              />
            </li>
          ))}
        </ul>
      )}

      {isCreating && (
        <form
          className={styles.inlineForm}
          onSubmit={(event) => {
            event.preventDefault()
            void submitNewLabel()
          }}
        >
          <label htmlFor={nameId} className={styles.fieldLabel}>
            New label name
          </label>
          <div className={styles.inlineFormRow}>
            <Input
              id={nameId}
              value={newName}
              size="sm"
              autoFocus
              invalid={formError !== null}
              aria-describedby={formError === null ? undefined : `${nameId}-error`}
              onChange={(event) => {
                setNewName(event.target.value)
                setFormError(null)
              }}
            />
            <Button type="submit" size="sm" loading={isBusy}>
              Create
            </Button>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => {
                setIsCreating(false)
                setNewName('')
                setFormError(null)
              }}
            >
              Cancel
            </Button>
          </div>
          {formError !== null && (
            <p id={`${nameId}-error`} role="alert" className={styles.formError}>
              {formError}
            </p>
          )}
          {/* The colour is the server's to choose: `LabelCreateInput` declares
            * `color: String = null` and assigns one. A picker here would be
            * this panel deciding a thing the product has not. */}
        </form>
      )}
    </Panel>
  )
}
