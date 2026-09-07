import { useCallback } from 'react'
import { useQuery } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { describeError } from '../../issues/lib/errors'
import { IssueTemplateListDocument } from './documents'
import type { IssueTemplate } from './types'

const NO_TEMPLATES: readonly IssueTemplate[] = []

export interface UseTemplateListResult {
  templates: readonly IssueTemplate[]
  isLoading: boolean
  errorMessage: string | null
  retry: () => void
}

/**
 * Every template in the workspace.
 *
 * Not paginated, because `issueTemplates` takes no page-size argument -- it
 * returns the whole list, which is the right shape for a set of forms a team
 * maintains by hand and the server's shape rather than a simplification made
 * here.
 *
 * `teamId` is not sent although the field accepts it: a template with no team
 * is workspace-wide, and narrowing the request to one team would hide exactly
 * those. The screen groups by team on the client from the `teamId` already on
 * each row.
 */
export function useTemplateList(): UseTemplateListResult {
  const workspaceSlug = useWorkspaceSlug()

  const { data, error, loading, refetch } = useQuery(IssueTemplateListDocument, {
    variables: { workspaceSlug },
  })

  const retry = useCallback(() => {
    // Swallowed: `refetch` rejects *and* sets `error` on the hook result, and
    // `error` is what the screen renders.
    void refetch().catch(() => undefined)
  }, [refetch])

  return {
    templates: data?.issueTemplates ?? NO_TEMPLATES,
    isLoading: loading,
    errorMessage: error === undefined ? null : describeError(error),
    retry,
  }
}
