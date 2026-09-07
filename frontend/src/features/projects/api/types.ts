/**
 * The names this feature knows the server contract by.
 *
 * Every type below is an *alias* of something in `src/generated/`. Nothing
 * here declares a field, a scalar or a nullability, so nothing here can
 * disagree with the schema: change ./operations.graphql, regenerate, and the
 * shapes these names refer to change with it.
 *
 * The generator names a type after the construct that produced it --
 * `ProjectListQuery`, `ProjectRowFieldsFragment` -- which says how a type was
 * made rather than what it is. It also gives no name at all to a *selection*,
 * and several of these are selections: `ProjectIssue` below is what the
 * `ProjectIssues` document asks for, which is a much narrower thing than the
 * schema's `Issue`. Indexing into the operation type is what keeps these
 * names meaning "what actually arrives".
 *
 * The line this file holds: server contract is generated, UI state is not.
 * `MutationOutcome` in ./mutations.ts is hand-written because no server
 * declares it.
 */

import type {
  ProjectCreateMutation,
  ProjectDetailFieldsFragment,
  ProjectDetailQuery,
  ProjectIssuesQuery,
  ProjectListQuery,
  ProjectMembersQuery,
  ProjectMilestoneFieldsFragment,
  ProjectRowFieldsFragment,
  ProjectTeamsQuery,
  ProjectUnfiledIssuesQuery,
} from '../../../generated/operations'

/**
 * The input types, re-exported unchanged.
 *
 * The only names here that refer to schema types rather than to selections:
 * an input has no selection set, so what the server declares and what the
 * client sends are the same shape by construction.
 */
export type {
  ProjectCreateInput,
  ProjectMilestoneCreateInput,
  ProjectMilestoneUpdateInput,
  ProjectState,
  ProjectUpdateInput,
} from '../../../generated/schema'

/** The fields a list row selects. `description` and `milestones` are not among them. */
export type ProjectRowFields = ProjectRowFieldsFragment

/** Everything the detail view shows, including the milestone list. */
export type ProjectDetailFields = ProjectDetailFieldsFragment

/** One milestone, as every screen that renders one receives it. */
export type ProjectMilestone = ProjectMilestoneFieldsFragment

/** One member of the workspace, as the lead picker and the lead label need them. */
export type ProjectMember = ProjectMembersQuery['workspaceMembers'][number]

/** One team of the workspace, as the membership editor needs it. */
export type ProjectTeam = ProjectTeamsQuery['teams'][number]

/**
 * One issue, as *this feature's* document selects it.
 *
 * Not the schema's `Issue` and not the issues feature's `IssueRowFields`: it
 * carries `identifier`, `projectId` and `milestoneId`, which are the three
 * fields a project screen needs and the issue list does not select.
 */
export type ProjectIssue = ProjectIssuesQuery['issues']['nodes'][number]

/**
 * One candidate for the "add an issue" menu: an issue in no project.
 *
 * Narrower than `ProjectIssue`, because a menu entry shows a name and nothing
 * else -- no `projectId`, which the filter has already answered, and no
 * `completedAt`, which nothing in a menu renders.
 */
export type ProjectUnfiledIssue = ProjectUnfiledIssuesQuery['issues']['nodes'][number]

/**
 * One entry of a payload's `errors`.
 *
 * These are *not* GraphQL errors. They arrive inside `data`, as a typed list,
 * because they describe user input rather than a failure of the request --
 * an empty name, a target date that will not parse -- and a payload's
 * `project` is null exactly when this list is non-empty.
 */
export type ProjectValidationError =
  ProjectCreateMutation['projectCreate']['errors'][number]

export type ProjectListData = ProjectListQuery
export type ProjectDetailData = ProjectDetailQuery
export type ProjectTeamsData = ProjectTeamsQuery
export type ProjectIssuesData = ProjectIssuesQuery

/** The connection as the list query selects it: `nodes`, never Relay's `edges`. */
export type ProjectConnection = ProjectListQuery['projects']
