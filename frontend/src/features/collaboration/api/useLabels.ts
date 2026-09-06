import { useCallback } from 'react'
import { useMutation, useQuery } from '@apollo/client/react'
import { NetworkStatus } from '@apollo/client'

import { describeError } from '../../issues/lib/errors'
import { insertWorkspaceLabel } from './cache'
import {
  IssueLabelAttachDocument,
  IssueLabelDetachDocument,
  IssueLabelsDocument,
  LabelCreateDocument,
  WorkspaceLabelsDocument,
} from './documents'
import { settle } from './outcome'
import type { MutationOutcome } from './outcome'
import { useLoadMore } from './paging'
import type { Label } from './types'

const NO_LABELS: readonly Label[] = []

export interface UseLabelsResult {
  /** The labels on this issue, as the server has them. */
  labels: readonly Label[]
  /**
   * Workspace labels not already on the issue.
   *
   * Filtered here rather than in the panel because "what can still be added"
   * is a fact about two server answers, and computing it in the component
   * would put it in whichever component asked first.
   */
  attachable: readonly Label[]
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
  /** The picker's list is paginated; the issue's own labels are not. */
  hasMoreLabels: boolean
  isLoadingMoreLabels: boolean
  loadMoreLabels: () => void
  attach: (labelId: string) => Promise<MutationOutcome<unknown>>
  detach: (labelId: string) => Promise<MutationOutcome<unknown>>
  /**
   * Create a label in the workspace and put it on this issue.
   *
   * Two mutations, in order, because the schema has no create-and-attach
   * operation. The outcome describes whichever step failed; a create that
   * succeeded and an attach that did not leaves a real label in the workspace
   * and says so, rather than pretending the whole thing rolled back.
   */
  createAndAttach: (name: string) => Promise<MutationOutcome<unknown>>
  isBusy: boolean
}

/** One issue's labels, and the workspace list they are chosen from. */
export function useLabels(workspaceSlug: string, issueId: string): UseLabelsResult {
  const { data, error, networkStatus, refetch } = useQuery(IssueLabelsDocument, {
    variables: { workspaceSlug, issueId },
    notifyOnNetworkStatusChange: true,
  })

  const {
    data: workspaceLabels,
    networkStatus: labelsStatus,
    fetchMore,
  } = useQuery(WorkspaceLabelsDocument, {
    variables: { workspaceSlug, after: null },
    notifyOnNetworkStatusChange: true,
  })

  const paging = useLoadMore(
    workspaceLabels?.labels.pageInfo,
    labelsStatus,
    (after) => fetchMore({ variables: { after } }),
  )

  // Neither attach nor detach gets an `update`: both payloads select the whole
  // `Issue` with its `labels`, so Apollo writes the server's own answer onto
  // the normalised `Issue:<uuid>` this query is watching. A hand-written
  // update here would be a second source of truth competing with the first.
  const [attachMutation, { loading: isAttaching }] = useMutation(IssueLabelAttachDocument)
  const [detachMutation, { loading: isDetaching }] = useMutation(IssueLabelDetachDocument)

  const [createMutation, { loading: isCreating }] = useMutation(LabelCreateDocument, {
    update(cache, result) {
      const label = result.data?.labelCreate.label

      if (label == null) {
        return
      }

      insertWorkspaceLabel(cache, workspaceSlug, label)
    },
  })

  const attach = useCallback(
    (labelId: string) =>
      settle(async () => {
        const result = await attachMutation({
          variables: { input: { workspaceSlug, issueId, labelId } },
        })

        return result.data?.issueLabelAttach
      }),
    [attachMutation, issueId, workspaceSlug],
  )

  const detach = useCallback(
    (labelId: string) =>
      settle(async () => {
        const result = await detachMutation({
          variables: { input: { workspaceSlug, issueId, labelId } },
        })

        return result.data?.issueLabelDetach
      }),
    [detachMutation, issueId, workspaceSlug],
  )

  const createAndAttach = useCallback(
    async (name: string): Promise<MutationOutcome<unknown>> => {
      const created = await settle(async () => {
        // `color` is left off: `LabelCreateInput` declares `color: String =
        // null` and the server assigns one. Inventing a hex here would be the
        // client deciding something the product has not.
        const result = await createMutation({
          variables: { input: { workspaceSlug, name } },
        })

        return result.data?.labelCreate
      })

      if (created.status !== 'ok') {
        return created
      }

      const label = created.payload.label

      if (label == null) {
        return { status: 'failed', message: 'The label was not created.' }
      }

      return attach(label.id)
    },
    [attach, createMutation, workspaceSlug],
  )

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  const labels = data?.issue?.labels ?? NO_LABELS
  const attached = new Set(labels.map((label) => label.id))

  return {
    labels,
    attachable:
      workspaceLabels?.labels.nodes.filter((label) => !attached.has(label.id)) ??
      NO_LABELS,
    isLoading: networkStatus === NetworkStatus.loading && data === undefined,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
    hasMoreLabels: paging.hasNextPage,
    isLoadingMoreLabels: paging.isLoadingMore,
    loadMoreLabels: paging.loadMore,
    attach,
    detach,
    createAndAttach,
    isBusy: isAttaching || isDetaching || isCreating,
  }
}
