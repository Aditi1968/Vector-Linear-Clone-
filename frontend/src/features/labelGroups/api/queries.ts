import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { GroupedLabelListDocument, LabelGroupListDocument } from './documents'
import type { GroupedLabel, LabelGroup } from './types'

/** Stable identities for "nothing yet", so memoised children are not defeated. */
const NO_GROUPS: readonly LabelGroup[] = []
const NO_LABELS: readonly GroupedLabel[] = []

export interface UseLabelGroupsResult {
  groups: readonly LabelGroup[]
  /** Every label the first page returned, grouped or not. */
  labels: readonly GroupedLabel[]
  /**
   * There are labels beyond the page in hand.
   *
   * Surfaced rather than hidden, because it changes what the screen is
   * entitled to claim: a group can only be shown holding the labels that were
   * loaded, so with more labels outstanding an empty group might not be empty.
   */
  hasMoreLabels: boolean
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * The workspace's label groups, and the labels they are made of.
 *
 * Two documents, deliberately not one. `labelGroups` is a plain list and
 * `labels` is a connection; combining them into a single query would price the
 * whole document at the connection's cost even for the screen's first paint,
 * and -- more to the point -- would prevent either from being served out of
 * the cache when another feature has already asked for it. The label picker in
 * `features/collaboration` sends the same `labels(workspaceSlug:, first: 50)`
 * read; both write the same cache field, and Apollo merges the two selections
 * onto each `Label:<uuid>`.
 *
 * ## Why membership is read from the labels and not from the groups
 *
 * `LabelGroup` has no member list. Migration 021 puts the membership on the
 * LABEL (`labels.group_id`) because that is where the exclusivity machinery
 * needs it -- the generated `exclusivity_key` must be computable from the
 * label's own row. So "which labels are in this group" is a question only the
 * label list can answer, which is why this hook returns both and ../lib
 * assembles them.
 *
 * ponytail: one page of 50 labels, and `hasMoreLabels` is what keeps that from
 * being a lie -- every claim the screen makes about membership is qualified by
 * it. The upgrade path is a `LabelGroup.labels` field, not a client that pages
 * `labels` to exhaustion: a taxonomy screen should not have to read every
 * label in a workspace to draw six groups.
 */
export function useLabelGroups(): UseLabelGroupsResult {
  const workspaceSlug = useWorkspaceSlug()

  const groupsResult = useQuery(LabelGroupListDocument, { variables: { workspaceSlug } })
  const labelsResult = useQuery(GroupedLabelListDocument, { variables: { workspaceSlug } })

  const retry = useCallback(() => {
    // Swallowed on purpose: `refetch` rejects *and* sets `error` on the hook
    // result, and `error` is what the screen renders.
    void groupsResult.refetch().catch(() => undefined)
    void labelsResult.refetch().catch(() => undefined)
  }, [groupsResult, labelsResult])

  // The groups' failure wins when both fail: without the groups there is no
  // screen, where without the labels there is a screen that cannot say what is
  // in each group. Reporting both would be two alerts about one outage.
  const error = groupsResult.error ?? labelsResult.error

  return {
    groups: groupsResult.data?.labelGroups ?? NO_GROUPS,
    labels: labelsResult.data?.labels.nodes ?? NO_LABELS,
    hasMoreLabels: labelsResult.data?.labels.pageInfo.hasNextPage ?? false,
    isLoading: groupsResult.loading || labelsResult.loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
