/**
 * The names this feature knows the server contract by.
 *
 * Every type below is an *alias* of something in `src/generated/`. Nothing
 * here declares a field, a scalar or a nullability, and nothing here can
 * disagree with the schema: change `operations.graphql`, regenerate, and the
 * shapes these names refer to change with it. `npm run graphql:check` fails
 * if they have not been regenerated.
 *
 * ## Why this file still exists
 *
 * Two reasons, and neither is habit.
 *
 * The generator names things after the GraphQL construct that produced them
 * -- `IssueListQuery`, `IssueRowFieldsFragment`, `IssueCreateMutationVariables`
 * -- which is the right convention for generated output and the wrong one to
 * spread through a UI codebase, because it says how a type was made rather
 * than what it is. `IssueRowFields` is what a row component needs; that it
 * arrives as a fragment is this directory's business.
 *
 * More importantly, several of these are *selections*, not schema types, and
 * the generator does not give a selection a name of its own. Indexing into
 * the operation type is what keeps `IssueProject` here meaning "what actually
 * arrives" -- and what makes it follow the document automatically if the
 * selection ever changes.
 *
 * ## What is deliberately *not* here
 *
 * UI state. `IssueSaveOutcome` in ./outcome.ts is a three-case union
 * describing what this application does about a write; no server declares it
 * and no generator could produce it, so it is hand-written and stays
 * hand-written. The line is: server contract is generated, UI state is not.
 */

import type {
  IssueArchiveMutation,
  IssueArchiveMutationVariables,
  IssueCreateMutation,
  IssueCreateMutationVariables,
  IssueDetailFieldsFragment,
  IssueDetailQuery,
  IssueDetailQueryVariables,
  IssueListQuery,
  IssueListQueryVariables,
  IssueRowFieldsFragment,
  IssueSetCycleMutation,
  IssueSetProjectMutation,
  IssueUpdateMutation,
  IssueUpdateMutationVariables,
  IssueWorkspaceContextQuery,
  IssueWorkspaceContextQueryVariables,
  TeamCyclesQuery,
  TeamCyclesQueryVariables,
  ValidationFieldsFragment,
} from '../../../generated/operations'
import type { IssueCreateInput as IssueCreateInputType } from '../../../generated/schema'

/**
 * The inputs, re-exported unchanged.
 *
 * The only names in this file that refer to schema types rather than to
 * selections: an input has no selection set, so what the server declares and
 * what the client sends are the same shape by construction.
 */
export type {
  IssueCreateInput,
  IssueSetCycleInput,
  IssueSetProjectInput,
  IssueUpdateInput,
  WorkflowStateCategory,
} from '../../../generated/schema'

/**
 * What the composer actually collects: `IssueCreateInput` minus the two
 * fields nobody types.
 *
 * `workspaceSlug` comes from the URL and `teamId` from the workspace context,
 * and ./useCreateIssue supplies both. Derived from the schema type with
 * `Omit` rather than declared, so a field added to `IssueCreateInput` appears
 * here automatically and a field REMOVED from it becomes a compile error in
 * this file rather than a silently ignored property on the wire.
 */
export type IssueDraft = Omit<IssueCreateInputType, 'workspaceSlug' | 'teamId'>

/**
 * The patch `issueUpdate` accepts, minus the workspace.
 *
 * Every field optional, because the mutation is a patch. Note that `null` is
 * not the same as absent: an omitted key leaves the field alone, an explicit
 * null clears it. ./useIssueMutations.ts is where that distinction is made.
 */
export type IssuePatch = Omit<
  IssueUpdateMutationVariables['input'],
  'workspaceSlug'
>

/**
 * The fields every list row selects.
 *
 * `description` is deliberately absent from the list document. A page of 25
 * rows does not display issue bodies, and fetching them costs both bandwidth
 * and complexity budget (see ./operations.graphql) for text no row renders.
 */
export type IssueRowFields = IssueRowFieldsFragment

/**
 * Everything the detail view shows.
 *
 * A superset of `IssueRowFields` because the fragment spreads it, which is
 * the type-level half of a guarantee ./operations.graphql makes at the
 * document level: every mutation that returns an issue selects a superset of
 * what the list selects, so a normalised mutation result can never leave the
 * cached list holding a partial entity.
 */
export type IssueDetailFields = IssueDetailFieldsFragment

/** `Issue.labels`, `Issue.project`, `Issue.cycle` as the row selects them. */
export type IssueLabel = IssueRowFields['labels'][number]
export type IssueProject = NonNullable<IssueRowFields['project']>
export type IssueCycle = NonNullable<IssueRowFields['cycle']>

export type IssueListData = IssueListQuery

/**
 * Only `after` is a variable.
 *
 * `first` is written into the document as a literal, for a reason documented
 * at length in ./operations.graphql: the backend's complexity rule charges a
 * page size it cannot read at validation time -- which is what a variable is
 * -- at 100 rather than at 25.
 */
export type IssueListVariables = IssueListQueryVariables

/**
 * The connection as this query selects it.
 *
 * `nodes`, not Relay's `edges`/`node`: the backend's `IssueConnection`
 * (`app/graphql/types/issue.py`) has no edge type at all.
 */
export type IssueConnection = IssueListQuery['issues']

/** `PageInfo` as the connection returns it -- no per-node cursors. */
export type IssuePageInfo = IssueConnection['pageInfo']

export type IssueDetailData = IssueDetailQuery
export type IssueDetailVariables = IssueDetailQueryVariables

export type IssueCreateData = IssueCreateMutation
export type IssueCreateVariables = IssueCreateMutationVariables
export type IssueUpdateData = IssueUpdateMutation
export type IssueUpdateVariables = IssueUpdateMutationVariables
export type IssueSetProjectData = IssueSetProjectMutation
export type IssueSetCycleData = IssueSetCycleMutation
export type IssueArchiveData = IssueArchiveMutation
export type IssueArchiveVariables = IssueArchiveMutationVariables

/** Null `issue` exactly when `errors` is non-empty. */
export type IssueCreatePayload = IssueCreateMutation['issueCreate']

/**
 * One entry of a payload's `errors`.
 *
 * These are *not* GraphQL errors. They arrive inside `data`, as a typed list,
 * because they describe user input rather than a failure of the request. The
 * `field`/`code` pairs are a stated public contract of
 * `app/services/issues.py`:
 *
 *     title    REQUIRED      length < 1
 *     title    TOO_LONG      length > 500
 *     priority OUT_OF_RANGE  outside 0..4
 *
 * and the service collects every violation before raising, so this list can
 * hold more than one entry and the form must render all of them.
 */
export type IssueValidationError = ValidationFieldsFragment

/* --------------------------------------------------- workspace context */

export type IssueWorkspaceContextData = IssueWorkspaceContextQuery
export type IssueWorkspaceContextVariables = IssueWorkspaceContextQueryVariables

/** One team, with the workflow states its issues can occupy. */
export type WorkspaceTeam = IssueWorkspaceContextQuery['teams'][number]

/**
 * One status on a team's board.
 *
 * Branch on `category`, never on `name`: the name belongs to the team and may
 * be anything, the category is the fixed meaning the server guarantees.
 */
export type WorkflowState = WorkspaceTeam['workflowStates'][number]

/** One person in the workspace. `Issue.assigneeId` is resolved through these. */
export type WorkspaceMember = IssueWorkspaceContextQuery['workspaceMembers'][number]

/** One project an issue can be placed in. */
export type WorkspaceProject =
  IssueWorkspaceContextQuery['projects']['nodes'][number]

export type TeamCyclesData = TeamCyclesQuery
export type TeamCyclesVariables = TeamCyclesQueryVariables

/** One cycle an issue can be placed in. */
export type TeamCycle = TeamCyclesQuery['cycles'][number]
