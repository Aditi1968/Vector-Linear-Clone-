/**
 * The names this feature knows the server contract by.
 *
 * Every type below is an *alias* of something in `src/generated/`. Nothing
 * here declares a field, a scalar or a nullability, so nothing here can
 * disagree with the schema: change ./operations.graphql, regenerate, and the
 * shapes these names refer to change with it. `npm run graphql:check` fails
 * if they have not been regenerated.
 *
 * The generator names types after the construct that produced them --
 * `IssueCommentsQuery`, `CommentFieldsFragment` -- which says how a type was
 * made rather than what it is. A thread component needs `Comment`; that it
 * arrives as a fragment is this directory's business.
 *
 * The line, as elsewhere in this codebase: server contract is generated, UI
 * state is not. The outcome unions in the hooks beside this file are
 * hand-written, because no server declares them.
 */

import type {
  CommentAuthorsQuery,
  CommentCreateMutation,
  CommentFieldsFragment,
  IssueCommentsQuery,
  IssueRelationsQuery,
  IssueSearchQuery,
  IssueSubIssuesQuery,
  IssueSummaryFieldsFragment,
  LabelFieldsFragment,
  WorkspaceLabelsQuery,
} from '../../../generated/operations'

export type { IssueRelationType } from '../../../generated/schema'

/** One comment, as the thread and the create mutation both select it. */
export type Comment = CommentFieldsFragment

/** One label, as the tags, the picker and the create mutation select it. */
export type Label = LabelFieldsFragment

/**
 * The other end of a parent, child or relation edge.
 *
 * `IssueSummary` and not `Issue`: it carries no `identifier`, so rows built
 * from one are named by title.
 */
export type IssueSummary = IssueSummaryFieldsFragment

/** One relation row: the edge's id and type, plus the issue on the far end. */
export type IssueRelation = NonNullable<
  IssueRelationsQuery['issue']
>['relations']['nodes'][number]

/** One search hit, which is the only place an `identifier` is available. */
export type IssueSearchHit = IssueSearchQuery['search']['issues'][number]

/** Everyone in the workspace, as comment authors are resolved through. */
export type WorkspaceMember = CommentAuthorsQuery['workspaceMembers'][number]

/**
 * One entry of a payload's `errors`.
 *
 * These are *not* GraphQL errors. They arrive inside `data`, as a typed list,
 * because they describe user input rather than a failure of the request --
 * an empty comment body, a label name already taken.
 */
export type ValidationError = CommentCreateMutation['commentCreate']['errors'][number]

/**
 * The connections as their documents select them.
 *
 * Indexed out of the operation types rather than named from the schema: the
 * schema's `CommentConnection` says `nodes: Array<Comment>` with all seven
 * fields, and ./operations.graphql asks for four. These names mean "what
 * actually arrives", and follow the documents automatically.
 */
export type CommentConnection = NonNullable<IssueCommentsQuery['issue']>['comments']
export type RelationConnection = NonNullable<IssueRelationsQuery['issue']>['relations']
export type ChildConnection = NonNullable<IssueSubIssuesQuery['issue']>['children']
export type LabelConnection = WorkspaceLabelsQuery['labels']

/** `PageInfo` as every connection returns it -- no per-node cursors. */
export type ConnectionPageInfo = CommentConnection['pageInfo']
