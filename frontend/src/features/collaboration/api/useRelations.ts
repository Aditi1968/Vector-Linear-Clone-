import { useCallback } from 'react'
import { NetworkStatus } from '@apollo/client'
import { useMutation, useQuery } from '@apollo/client/react'

import { describeError } from '../../issues/lib/errors'
import { prependRelation, removeRelation } from './cache'
import {
  IssueRelationCreateDocument,
  IssueRelationDeleteDocument,
  IssueRelationsDocument,
} from './documents'
import { settle } from './outcome'
import type { MutationOutcome } from './outcome'
import { useLoadMore } from './paging'
import type { IssueRelation, IssueRelationType } from './types'

const NO_RELATIONS: readonly IssueRelation[] = []

export interface UseRelationsResult {
  relations: readonly IssueRelation[]
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
  hasNextPage: boolean
  isLoadingMore: boolean
  loadMoreErrorMessage: string | null
  loadMore: () => void
  createRelation: (
    targetIssueId: string,
    type: IssueRelationType,
  ) => Promise<MutationOutcome<unknown>>
  deleteRelation: (relationId: string) => Promise<MutationOutcome<unknown>>
  isBusy: boolean
}

/**
 * One issue's relations to other issues.
 *
 * The relation type is sent exactly as the picker chose it and rendered
 * exactly as the server returns it -- there is no client-side inversion
 * anywhere in this feature. `BLOCKS` and `BLOCKED_BY` are two directions of
 * one edge and the server owns which end this issue is on; a panel that
 * flipped them "to be helpful" would show the opposite of the truth on the
 * other issue's screen.
 */
export function useRelations(workspaceSlug: string, issueId: string): UseRelationsResult {
  const { data, error, networkStatus, fetchMore, refetch } = useQuery(
    IssueRelationsDocument,
    {
      variables: { workspaceSlug, issueId, after: null },
      notifyOnNetworkStatusChange: true,
    },
  )

  const connection = data?.issue?.relations

  const paging = useLoadMore(connection?.pageInfo, networkStatus, (after) =>
    fetchMore({ variables: { after } }),
  )

  const [create, { loading: isCreating }] = useMutation(IssueRelationCreateDocument, {
    update(cache, result) {
      const relation = result.data?.issueRelationCreate.relation

      if (relation == null) {
        return
      }

      prependRelation(cache, workspaceSlug, issueId, relation)
    },
  })

  const [destroy, { loading: isDeleting }] = useMutation(IssueRelationDeleteDocument, {
    update(cache, result) {
      const deletedRelationId = result.data?.issueRelationDelete.deletedRelationId

      if (deletedRelationId == null) {
        return
      }

      removeRelation(cache, workspaceSlug, issueId, deletedRelationId)
    },
  })

  const createRelation = useCallback(
    (targetIssueId: string, type: IssueRelationType) =>
      settle(async () => {
        const result = await create({
          variables: {
            // `sourceIssueId` is always the issue on screen. The panel never
            // relates two issues neither of which it is showing.
            input: { workspaceSlug, sourceIssueId: issueId, targetIssueId, type },
          },
        })

        return result.data?.issueRelationCreate
      }),
    [create, issueId, workspaceSlug],
  )

  const deleteRelation = useCallback(
    (relationId: string) =>
      settle(async () => {
        const result = await destroy({
          variables: { input: { workspaceSlug, id: relationId } },
        })

        return result.data?.issueRelationDelete
      }),
    [destroy, workspaceSlug],
  )

  const retry = useCallback(() => {
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    relations: connection?.nodes ?? NO_RELATIONS,
    isLoading: networkStatus === NetworkStatus.loading && connection === undefined,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
    ...paging,
    createRelation,
    deleteRelation,
    isBusy: isCreating || isDeleting,
  }
}
