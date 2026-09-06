import { useCallback } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useMutation, useQuery } from '@apollo/client/react'

import { describeError } from '../../issues/lib/errors'
import { insertChild, removeChild } from './cache'
import {
  IssueClearParentDocument,
  IssueSetParentDocument,
  IssueSubIssuesDocument,
} from './documents'
import { settle } from './outcome'
import type { MutationOutcome } from './outcome'
import { useLoadMore } from './paging'
import type { IssueSummary } from './types'

const NO_CHILDREN: readonly IssueSummary[] = []

export interface UseSubIssuesResult {
  parent: IssueSummary | null
  children: readonly IssueSummary[]
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
  hasNextPage: boolean
  isLoadingMore: boolean
  loadMoreErrorMessage: string | null
  loadMore: () => void
  /** Put this issue under another one. */
  setParent: (parentId: string) => Promise<MutationOutcome<unknown>>
  /** Take this issue out from under its parent. */
  clearParent: () => Promise<MutationOutcome<unknown>>
  /** Adopt an existing issue as a sub-issue of this one. */
  addChild: (childId: string) => Promise<MutationOutcome<unknown>>
  /** Release a sub-issue. The issue itself is untouched -- only the edge goes. */
  removeChildIssue: (childId: string) => Promise<MutationOutcome<unknown>>
  isBusy: boolean
}

/**
 * An issue's place in the hierarchy, in both directions.
 *
 * ## One mutation pair, four controls
 *
 * `issueSetParent`/`issueClearParent` take the CHILD's id, always. That makes
 * "choose my parent" and "adopt a sub-issue" the same mutation with the two
 * ids swapped, which is easy to get backwards and impossible to notice
 * afterwards -- the wrong call succeeds and quietly builds the opposite
 * hierarchy. The four functions below exist so that no component ever has to
 * decide which id goes where.
 *
 * ## Why two of them need a cache update and two do not
 *
 * `setParent`/`clearParent` change THIS issue, so the payload returns this
 * issue with its new `parent` and Apollo's normalised write is the whole
 * update. `addChild`/`removeChildIssue` change SOMEBODY ELSE -- the payload
 * describes the child, not this issue -- so this issue's `children`
 * connection is a cache field nothing in the response mentions, and
 * ./cache.ts has to write it.
 */
export function useSubIssues(workspaceSlug: string, issueId: string): UseSubIssuesResult {
  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    IssueSubIssuesDocument,
    {
      variables: { workspaceSlug, issueId, after: null },
      notifyOnNetworkStatusChange: true,
    },
  )

  const connection = data?.issue?.children

  const paging = useLoadMore(connection?.pageInfo, networkStatus, (after) =>
    fetchMore({ variables: { after } }),
  )

  const [setParentMutation, { loading: isSetting }] = useMutation(IssueSetParentDocument)
  const [clearParentMutation, { loading: isClearing }] = useMutation(
    IssueClearParentDocument,
  )

  const setParent = useCallback(
    (parentId: string) =>
      settle(async () => {
        const result = await setParentMutation({
          // No `update`: `issueId` is this issue, so the payload's `parent`
          // lands on the normalised entity this query already watches.
          variables: { input: { workspaceSlug, issueId, parentId } },
        })

        return result.data?.issueSetParent
      }),
    [issueId, setParentMutation, workspaceSlug],
  )

  const clearParent = useCallback(
    () =>
      settle(async () => {
        const result = await clearParentMutation({
          variables: { input: { workspaceSlug, issueId } },
        })

        return result.data?.issueClearParent
      }),
    [clearParentMutation, issueId, workspaceSlug],
  )

  const addChild = useCallback(
    (childId: string) =>
      settle(async () => {
        const result = await setParentMutation({
          // The child is the subject; this issue is the parent it gains.
          variables: { input: { workspaceSlug, issueId: childId, parentId: issueId } },
          update(cache, mutationResult) {
            const child = mutationResult.data?.issueSetParent.issue

            if (child == null) {
              return
            }

            // The payload is an `Issue` and `children` holds `IssueSummary`,
            // which are two different normalised entities for one row. The
            // fields are selected on the mutation precisely so this mapping
            // invents nothing -- see the note on `IssueSetParent` in
            // ./operations.graphql, and keep the two selections in step.
            insertChild(cache, workspaceSlug, issueId, {
              __typename: 'IssueSummary',
              id: child.id,
              title: child.title,
              completedAt: child.completedAt,
              createdAt: child.createdAt,
            })
          },
        })

        return result.data?.issueSetParent
      }),
    [issueId, setParentMutation, workspaceSlug],
  )

  const removeChildIssue = useCallback(
    (childId: string) =>
      settle(async () => {
        const result = await clearParentMutation({
          variables: { input: { workspaceSlug, issueId: childId } },
          update(cache, mutationResult) {
            if (mutationResult.data?.issueClearParent.issue == null) {
              return
            }

            removeChild(cache, workspaceSlug, issueId, childId)
          },
        })

        return result.data?.issueClearParent
      }),
    [clearParentMutation, issueId, workspaceSlug],
  )

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    parent: data?.issue?.parent ?? null,
    children: connection?.nodes ?? NO_CHILDREN,
    isLoading: networkStatus === NetworkStatus.loading && data === undefined,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
    ...paging,
    setParent,
    clearParent,
    addChild,
    removeChildIssue,
    isBusy: isSetting || isClearing,
  }
}
