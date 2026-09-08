import { useCallback } from 'react'
import { useMutation } from '@apollo/client/react'

import { useWorkspaceSlug } from '../../../app/routes'
import { readPayload } from '../../../lib/graphql'
import type { PayloadOutcome } from '../../../lib/graphql'
import { describeError } from '../../issues/lib/errors'
import {
  IssueCreateFromTemplateDocument,
  IssueTemplateCreateDocument,
  IssueTemplateDeleteDocument,
  IssueTemplateUpdateDocument,
} from './documents'
import type {
  CreatedIssue,
  IssueTemplate,
  IssueTemplateDraft,
  TemplateValidationError,
} from './types'

/**
 * What a template write can do, as three cases that cannot be confused.
 *
 * See `src/lib/graphql/payload.ts` for the reader.
 */
export type TemplateOutcome<T> = PayloadOutcome<T, TemplateValidationError>

export interface UseTemplateActionsResult {
  createTemplate: (draft: IssueTemplateDraft) => Promise<TemplateOutcome<IssueTemplate>>
  /**
   * Replace a template.
   *
   * The draft is the WHOLE row, not a patch: `IssueTemplateUpdateInput.template`
   * is an `IssueTemplateFieldsInput!` and every field absent from it takes its
   * schema default. A caller that built the draft from anything less than the
   * loaded template would clear the fields it left out.
   */
  updateTemplate: (
    id: string,
    draft: IssueTemplateDraft,
  ) => Promise<TemplateOutcome<IssueTemplate>>
  deleteTemplate: (id: string) => Promise<TemplateOutcome<string>>
  /** File a real issue from a template, into a team, optionally retitled. */
  createIssue: (
    templateId: string,
    teamId: string,
    title: string | null,
  ) => Promise<TemplateOutcome<CreatedIssue>>
  isSaving: boolean
}

/**
 * Every write the templates screen makes.
 *
 * ## Which of these needs cache help
 *
 * `issueTemplateUpdate` returns the whole fragment over the same
 * `IssueTemplate:<id>` entity the list holds, so the row corrects itself by
 * normalisation. Nothing to do.
 *
 * Create and delete change the membership of `issueTemplates`, a plain list
 * field neither payload contains, so both refetch by operation name.
 *
 * `issueCreateFromTemplate` touches no template at all -- it produces an
 * `Issue` -- so it refetches nothing here. The issue lists it does affect
 * belong to `features/issues`, and reaching across to refetch a stranger's
 * query from here would be this feature deciding how that one caches.
 */
export function useTemplateActions(): UseTemplateActionsResult {
  const workspaceSlug = useWorkspaceSlug()

  const refetchQueries = ['IssueTemplateList']

  const [create, createState] = useMutation(IssueTemplateCreateDocument, { refetchQueries })
  const [update, updateState] = useMutation(IssueTemplateUpdateDocument)
  const [remove, removeState] = useMutation(IssueTemplateDeleteDocument, { refetchQueries })
  const [fromTemplate, fromTemplateState] = useMutation(IssueCreateFromTemplateDocument)

  const createTemplate = useCallback(
    async (draft: IssueTemplateDraft) => {
      try {
        const result = await create({
          variables: { input: { workspaceSlug, template: draft } },
        })

        return readPayload(
          result.data?.issueTemplateCreate,
          result.data?.issueTemplateCreate.template,
        )
      } catch (reason) {
        // The default `errorPolicy` of `none` makes `mutate` reject on a
        // top-level GraphQL error as well as on a transport failure.
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [create, workspaceSlug],
  )

  const updateTemplate = useCallback(
    async (id: string, draft: IssueTemplateDraft) => {
      try {
        const result = await update({
          variables: { input: { workspaceSlug, id, template: draft } },
        })

        return readPayload(
          result.data?.issueTemplateUpdate,
          result.data?.issueTemplateUpdate.template,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [update, workspaceSlug],
  )

  const deleteTemplate = useCallback(
    async (id: string) => {
      try {
        const result = await remove({ variables: { input: { workspaceSlug, id } } })
        const payload = result.data?.issueTemplateDelete

        // The payload's id field is `id`, not `deletedTemplateId` -- unlike
        // its two neighbours in this schema. Read as the schema declares it.
        return readPayload(payload, payload?.id)
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [remove, workspaceSlug],
  )

  const createIssue = useCallback(
    async (templateId: string, teamId: string, title: string | null) => {
      try {
        const result = await fromTemplate({
          variables: { input: { workspaceSlug, templateId, teamId, title } },
        })

        return readPayload(
          result.data?.issueCreateFromTemplate,
          result.data?.issueCreateFromTemplate.issue,
        )
      } catch (reason) {
        return { status: 'failed' as const, message: describeError(reason) }
      }
    },
    [fromTemplate, workspaceSlug],
  )

  return {
    createTemplate,
    updateTemplate,
    deleteTemplate,
    createIssue,
    isSaving:
      createState.loading ||
      updateState.loading ||
      removeState.loading ||
      fromTemplateState.loading,
  }
}
