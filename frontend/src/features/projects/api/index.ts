/**
 * The projects data adapter.
 *
 * The boundary the rest of the feature is written against. Screens and panels
 * import hooks and types from here; they never import `gql`, a document, an
 * Apollo hook, or an Apollo error type. When the backend grows an
 * `issues(projectId:)` filter -- the one thing this feature most wants -- the
 * change lands in this directory and stops here.
 *
 * The documents are exported too, and only from this module rather than from
 * the feature root, because mocking a GraphQL response requires the exact
 * document that produced it. That is a legitimate need of a test file and not
 * a licence for a component to run its own query.
 */

export {
  useProjectDetail,
  useProjectIssues,
  useProjectList,
  useProjectMembers,
  useProjectTeams,
  useUnfiledIssues,
} from './queries'
export type {
  UseProjectDetailResult,
  UseProjectIssuesResult,
  UseProjectListResult,
  UseProjectMembersResult,
  UseProjectTeamsResult,
} from './queries'

export { useCreateProject, useProjectActions } from './mutations'
export type {
  MilestoneDraft,
  ProjectDraft,
  ProjectOutcome,
  UseCreateProjectResult,
  UseProjectActionsResult,
} from './mutations'

export { describeError } from './errors'

export {
  IssueSetProjectDocument,
  ProjectCreateDocument,
  ProjectDeleteDocument,
  ProjectDetailDocument,
  ProjectIssuesDocument,
  ProjectListDocument,
  ProjectMembersDocument,
  ProjectMilestoneCreateDocument,
  ProjectMilestoneDeleteDocument,
  ProjectMilestoneUpdateDocument,
  ProjectTeamAddDocument,
  ProjectTeamRemoveDocument,
  ProjectTeamsDocument,
  ProjectUnfiledIssuesDocument,
  ProjectUpdateDocument,
} from './documents'

export type {
  ProjectConnection,
  ProjectCreateInput,
  ProjectDetailData,
  ProjectDetailFields,
  ProjectIssue,
  ProjectIssuesData,
  ProjectListData,
  ProjectMember,
  ProjectMilestone,
  ProjectRowFields,
  ProjectState,
  ProjectTeam,
  ProjectTeamsData,
  ProjectUnfiledIssue,
  ProjectUpdateInput,
  ProjectValidationError,
} from './types'
