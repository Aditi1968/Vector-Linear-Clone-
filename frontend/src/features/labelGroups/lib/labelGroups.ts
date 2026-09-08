/**
 * The derivation rules the label-group screens share.
 *
 * Everything here is a pure function of what the server sent. Nothing invents
 * a value the API did not supply, and nothing here enforces the exclusivity
 * rule -- that is PostgreSQL's, through a partial unique index, and a copy of
 * it in the client would be a second answer that could disagree.
 */

import type { GroupedLabel, LabelGroup } from '../api'

/**
 * The labels held by each group, keyed by group id.
 *
 * `LabelGroup` publishes no member list, deliberately: migration 021 puts the
 * membership on the LABEL, because the generated `exclusivity_key` that the
 * rule is written on must be computable from the label's own row. So this is
 * the only place "which labels are in this group" can be answered from, and it
 * is answered from whatever labels were loaded -- see `hasMoreLabels` on the
 * hook, and the sentence the screen puts beside an empty group.
 *
 * A label naming a group that is not in `groups` keeps its id and is NOT
 * silently promoted to ungrouped: both lists come from the same workspace in
 * the same paint, so a mismatch means something is out of step and the screen
 * should be able to say so rather than quietly disagreeing with the server.
 */
export function labelsByGroup(
  labels: readonly GroupedLabel[],
): ReadonlyMap<string, GroupedLabel[]> {
  const byGroup = new Map<string, GroupedLabel[]>()

  for (const label of labels) {
    if (label.groupId === null) {
      continue
    }

    const held = byGroup.get(label.groupId)

    if (held === undefined) {
      byGroup.set(label.groupId, [label])
    } else {
      held.push(label)
    }
  }

  return byGroup
}

/**
 * The labels that belong to no group.
 *
 * Null `groupId` is a real state and not a missing value -- migration 021 is
 * explicit that most labels belong to no group -- so these are the candidates
 * a group can be filled from, and they are shown as a set of their own rather
 * than as leftovers.
 */
export function ungroupedLabels(labels: readonly GroupedLabel[]): GroupedLabel[] {
  return labels.filter((label) => label.groupId === null)
}

/**
 * What exclusivity means, in the words a person needs before choosing it.
 *
 * Two sentences, not one word, because the choice is not reversible for free:
 * turning it ON is refused outright for a group whose labels already share an
 * issue, and that refusal comes from the database rather than from a check
 * this screen could run first.
 */
export const EXCLUSIVITY_HELP = {
  exclusive:
    'An issue may wear at most one label from this group. Attaching a second is refused by the database, not by a check that could be forgotten.',
  shared: 'An issue may wear any number of labels from this group.',
} as const

export function exclusivityLabel(group: Pick<LabelGroup, 'exclusive'>): string {
  return group.exclusive ? 'One at a time' : 'Any number'
}
