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
 * the generator does not give a selection a name of its own. The connection
 * as `IssueList` selects it is not `IssueConnection` from the schema: the
 * schema type says `nodes: Array<Issue>` with all seven fields, and the query
 * asks for six. Indexing into the operation type is what keeps
 * `IssueConnection` here meaning "what actually arrives" -- and what makes it
 * follow the document automatically if the selection ever changes.
 *
 * ## What is deliberately *not* here
 *
 * UI state. `CreateIssueOutcome` in ./useCreateIssue is a three-case union
 * describing what this application does about a create; no server declares
 * it and no generator could produce it, so it is hand-written and stays
 * hand-written. The line is: server contract is generated, UI state is not.
 */

import type {
  IssueCreateMutation,
  IssueCreateMutationVariables,
  IssueDetailFieldsFragment,
  IssueDetailQuery,
  IssueDetailQueryVariables,
  IssueListQuery,
  IssueListQueryVariables,
  IssueRowFieldsFragment,
} from '../../../generated/operations'

/**
 * `IssueCreateInput` as the schema declares it, re-exported unchanged.
 *
 * The one input type in this file, and the only one of these names that
 * refers to a schema type rather than to a selection: an input has no
 * selection set, so what the server declares and what the client sends are
 * the same shape by construction.
 *
 * `description` and `priority` are optional here where they were required in
 * the hand-written type that preceded this. That is the schema being read
 * correctly rather than a relaxation: `IssueCreateInput` declares
 * `description: String = null` and `priority: Int! = 0`, so both have server
 * defaults and neither has to be sent. ../components/IssueComposer sends all
 * three regardless, which is still valid.
 */
export type { IssueCreateInput } from '../../../generated/schema'

/**
 * The fields every list row selects.
 *
 * `description` is deliberately absent from the document. A list of 25 rows
 * does not display issue bodies, and fetching them costs both bandwidth and
 * complexity budget (see ./operations.graphql) for text no row renders. The
 * detail view selects it.
 */
export type IssueRowFields = IssueRowFieldsFragment

/**
 * Everything the detail view shows: all seven fields on `Issue`.
 *
 * A superset of `IssueRowFields` because the fragment spreads it, which is
 * the type-level half of a guarantee ./operations.graphql makes at the
 * document level: the create mutation selects a superset of what the list
 * selects, so an issue written into the cache by the mutation can satisfy a
 * later read of the list query.
 */
export type IssueDetailFields = IssueDetailFieldsFragment

export type IssueListData = IssueListQuery

/**
 * Only `after` is a variable.
 *
 * `first` is written into the document as a literal, for a reason that is
 * documented at length in ./operations.graphql and is not a style
 * preference: the backend's complexity rule charges a page size it cannot
 * read at validation time -- which is what a variable is -- at 100 rather
 * than at 25.
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

/** Null `issue` exactly when `errors` is non-empty. */
export type IssueCreatePayload = IssueCreateMutation['issueCreate']

/**
 * One entry of `IssueCreatePayload.errors`.
 *
 * These are *not* GraphQL errors. They arrive inside `data`, as a typed list,
 * because they describe user input rather than a failure of the request. The
 * `field`/`code` pairs are a stated public contract of
 * `app/services/issues.py::_validate_create`:
 *
 *     title    REQUIRED      length < 1
 *     title    TOO_LONG      length > 500
 *     priority OUT_OF_RANGE  outside 0..4
 *
 * and the service collects every violation before raising, so this list can
 * hold more than one entry and the form must render all of them.
 */
export type IssueValidationError = IssueCreatePayload['errors'][number]
