/**
 * The two team screens.
 *
 *     { path: ROUTE_SEGMENTS.team,       element: <TeamPage /> }
 *     { path: ROUTE_SEGMENTS.teamIssues, element: <TeamIssuesPage /> }
 *
 * Both are addressed by team *key* -- the ENG in ENG-42 -- and resolve it
 * through `teams(workspaceSlug:)`, because no root field accepts one. The
 * data layer is ./api.ts and is reachable from `features/teams/api` when a
 * test needs a document to mock, but never through this module.
 */
export { TeamPage } from './TeamPage'
export { TeamIssuesPage } from './TeamIssuesPage'
