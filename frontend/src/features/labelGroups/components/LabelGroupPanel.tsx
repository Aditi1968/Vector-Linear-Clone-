import { useId, useState } from 'react'
import type { CSSProperties } from 'react'

import { Badge, Button, Select } from '../../../components'
import type { GroupedLabel, LabelGroup } from '../api'
import { EXCLUSIVITY_HELP, exclusivityLabel } from '../lib/labelGroups'
import styles from '../labelGroups.module.css'

export interface LabelGroupPanelProps {
  group: LabelGroup
  /** The labels currently in this group, from the page of labels in hand. */
  members: readonly GroupedLabel[]
  /** Labels belonging to no group, which is what one can be filled from. */
  available: readonly GroupedLabel[]
  /** There are labels beyond the loaded page, so the two lists are partial. */
  hasMoreLabels: boolean
  isSaving: boolean
  onEdit: () => void
  onDelete: () => void
  onAddLabel: (labelId: string) => void
  onRemoveLabel: (labelId: string) => void
}

/**
 * One group: what it means, what is in it, and what can go in.
 *
 * ## The picker offers only ungrouped labels, and that is the schema's shape
 *
 * `labelSetGroup` would happily move a label straight from one group to
 * another -- `groupId` is just a value. Offering that here would let somebody
 * empty a group they were not looking at, from a menu that never named it. A
 * label leaves its group on the group's own panel, where the consequence is
 * visible, and then becomes available everywhere.
 *
 * ## Neither list is a complete answer, and the panel says so
 *
 * Membership lives on the LABEL (`labels.group_id`), because the exclusivity
 * key migration 021 generates has to be computable from the label's own row.
 * So "what is in this group" is assembled from the labels that were loaded --
 * one page of 50 -- and a group can only be shown holding those. With more
 * outstanding, an apparently empty group might not be empty, and a sentence
 * beside it says exactly that rather than letting the gap pass as a fact.
 */
export function LabelGroupPanel({
  group,
  members,
  available,
  hasMoreLabels,
  isSaving,
  onEdit,
  onDelete,
  onAddLabel,
  onRemoveLabel,
}: LabelGroupPanelProps) {
  const fieldId = useId()
  const [pending, setPending] = useState('')

  return (
    <section aria-labelledby="label-group-panel-title" className={styles.panel}>
      <div className={styles.panelHead}>
        <h2 className={styles.panelTitle} id="label-group-panel-title">
          {group.name}
        </h2>
        <Badge tone={group.exclusive ? 'warning' : 'neutral'}>
          {exclusivityLabel(group)}
        </Badge>
      </div>

      <p className={styles.hint}>
        {group.exclusive ? EXCLUSIVITY_HELP.exclusive : EXCLUSIVITY_HELP.shared}
      </p>

      <div className={styles.panelActions}>
        <Button disabled={isSaving} onClick={onEdit} variant="secondary">
          Edit group…
        </Button>
        <Button disabled={isSaving} onClick={onDelete} variant="danger">
          Delete group
        </Button>
      </div>

      <p className={styles.hint}>
        Deleting the group keeps its labels and only removes the grouping. Nothing an
        issue is wearing is lost.
      </p>

      <h3 className={styles.sectionTitle}>
        Labels in this group <span className={styles.count}>{members.length}</span>
      </h3>

      {members.length === 0 ? (
        <p className={styles.hint}>
          {hasMoreLabels
            ? 'None among the labels loaded. There are more labels in this workspace than one page, so this group may hold some of them.'
            : 'None yet. A group with no labels imposes no rule.'}
        </p>
      ) : (
        <ul className={styles.labelList}>
          {members.map((label) => (
            <li className={styles.labelItem} key={label.id}>
              {/* The label's own colour, as an inline custom property, because
                  it is data from the server and not a design decision -- the
                  stylesheet still owns the swatch's size and shape. */}
              <span
                aria-hidden="true"
                className={styles.swatch}
                style={{ '--label-color': label.color } as CSSProperties}
              />
              <span className={styles.labelName}>{label.name}</span>
              <Button
                disabled={isSaving}
                onClick={() => {
                  onRemoveLabel(label.id)
                }}
                size="sm"
                variant="ghost"
              >
                {/* Named with the label, so no two of these buttons share an
                    accessible name. Label names are unique per workspace,
                    case-insensitively, so this is unique by construction. */}
                Remove {label.name}
              </Button>
            </li>
          ))}
        </ul>
      )}

      <h3 className={styles.sectionTitle}>Add a label</h3>

      {available.length === 0 ? (
        <p className={styles.hint}>
          Every label loaded already belongs to a group. A label leaves its current group
          from that group’s own panel.
        </p>
      ) : (
        <div className={styles.addRow}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${fieldId}-label`}>
              Ungrouped label
            </label>
            <Select
              id={`${fieldId}-label`}
              onChange={(event) => {
                setPending(event.target.value)
              }}
              value={pending}
            >
              <option value="">Choose a label…</option>
              {available.map((label) => (
                <option key={label.id} value={label.id}>
                  {label.name}
                </option>
              ))}
            </Select>
          </div>
          <Button
            disabled={isSaving || pending === ''}
            onClick={() => {
              onAddLabel(pending)
              setPending('')
            }}
            variant="secondary"
          >
            Add to group
          </Button>
        </div>
      )}

      {hasMoreLabels && (
        <p className={styles.hint}>
          This workspace has more labels than the page loaded here, so the menu above is
          not every ungrouped label.
        </p>
      )}
    </section>
  )
}
