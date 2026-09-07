import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { BoardLabelsDocument } from './documents'
import type { BoardLabelsQuery } from '../../../generated/operations'

/** One label, as this picker needs it: an id and something to call it. */
export type BoardLabel = BoardLabelsQuery['labels']['nodes'][number]

/** Stable identity for "none yet", so the options memo does not churn. */
const NO_LABELS: readonly BoardLabel[] = []

/**
 * Every label in the workspace, for the label picker.
 *
 * The workspace's labels and not the loaded cards': now that the server does
 * the filtering, a picker built from the cards would offer only the label
 * already selected, and could not be changed without being cleared. A failure
 * is not surfaced -- a label list that does not arrive costs one picker its
 * options, and taking the board down over it would be the worse answer.
 */
export function useBoardLabels(): readonly BoardLabel[] {
  const workspaceSlug = useWorkspaceSlug()

  const { data } = useQuery(BoardLabelsDocument, { variables: { workspaceSlug } })

  return data?.labels.nodes ?? NO_LABELS
}
