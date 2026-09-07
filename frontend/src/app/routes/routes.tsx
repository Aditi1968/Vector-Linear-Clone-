import type { RouteObject } from 'react-router-dom'

import { RequireAuth, authRoutes } from '../../features/auth'
import { CycleDetailPage, CycleListPage } from '../../features/cycles'
import { InboxPage } from '../../features/inbox'
import { IssueDetailPage, IssueListPage } from '../../features/issues'
import { MembersPage } from '../../features/members'
import { MyIssuesPage } from '../../features/myIssues'
import { onboardingRoutes } from '../../features/onboarding'
import { ProjectDetailPage, ProjectListPage } from '../../features/projects'
import { TeamIssuesPage, TeamPage } from '../../features/teams'
import { AppLayout } from '../layout'
import { NotFound } from './NotFound'
import { Placeholder } from './Placeholder'
import { ROUTE_SEGMENTS, WORKSPACE_SLUG_PARAM } from './paths'
import { RouteError } from './RouteError'

/**
 * The route table, composed from three sources.
 *
 *   - `authRoutes` (`features/auth`): the public surface -- `/`, `/login`,
 *     `/register`.
 *   - `onboardingRoutes` (`features/onboarding`): `/onboarding`, the one
 *     authenticated screen outside a workspace, because it is how you get
 *     your first one.
 *   - the workspace shell below: everything else, behind `<RequireAuth>`.
 *
 * Order is not what decides between them. React Router ranks a static segment
 * above a dynamic one, so `/login` and `/onboarding` match their own routes
 * and never `/:workspaceSlug` -- which is also why no workspace may be
 * slugged `login` or `onboarding`, a constraint the server owns.
 *
 * ## Why every page is a child of one parent route
 *
 * The children are declared as *relative* segments, so React Router resolves
 * them against `/:workspaceSlug` and the shell mounts once for all of them.
 * Chrome that unmounts and remounts loses focus and scroll position on every
 * navigation, which is the difference between an application and a website.
 * Components never see either form of the path, because they navigate through
 * `useAppPaths()`.
 *
 * ## Screens that are not built yet
 *
 * Several entries below render `Placeholder`, and that is deliberate. The
 * route is real, the URL is the one `paths` builds, the sidebar entry that
 * reaches it is real, and the screen says outright that it does not exist
 * yet. Swapping the `element` is the whole job of the agent who builds one --
 * see ./Placeholder.tsx.
 *
 * Exported as data rather than JSX elements so tests can mount a subtree with
 * a memory router without booting the whole application.
 */
export const routes: RouteObject[] = [
  ...authRoutes,
  ...onboardingRoutes,
  {
    path: `/:${WORKSPACE_SLUG_PARAM}`,

    /*
      `RequireAuth` outside the shell rather than inside it. The shell's first
      act is to ask for `myWorkspaces`, and an unauthenticated caller gets
      UNAUTHENTICATED from it -- which the shell would then have to render as
      "could not load your workspaces", a wrong and faintly alarming answer to
      "you are signed out". Gating above means the request is never sent.
    */
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),

    // Without this, React Router's own fallback renders the caught error's
    // stack trace into the production bundle. See ./RouteError.
    errorElement: <RouteError />,

    children: [
      {
        // A workspace with no page named is its issue list, which is the
        // product's front door. Rendered rather than redirected: there is
        // nothing to resolve here, so a redirect would only put a URL in
        // history that immediately leaves again.
        index: true,
        element: <IssueListPage />,
      },
      { path: ROUTE_SEGMENTS.issues, element: <IssueListPage /> },
      { path: ROUTE_SEGMENTS.issueDetail, element: <IssueDetailPage /> },
      {
        path: ROUTE_SEGMENTS.myIssues,
        element: <MyIssuesPage />,
      },
      {
        path: ROUTE_SEGMENTS.inbox,
        element: <InboxPage />,
      },
      {
        path: ROUTE_SEGMENTS.team,
        element: <TeamPage />,
      },
      {
        path: ROUTE_SEGMENTS.teamIssues,
        element: <TeamIssuesPage />,
      },
      {
        path: ROUTE_SEGMENTS.members,
        element: <MembersPage />,
      },
      {
        path: ROUTE_SEGMENTS.settings,
        element: (
          <Placeholder
            title="Settings"
            description="Workspace settings, including the GitHub and Slack integrations."
          />
        ),
      },
      {
        path: ROUTE_SEGMENTS.search,
        element: (
          <Placeholder
            title="Search"
            description="Search this workspace's issues and projects."
          />
        ),
      },
      {
        path: ROUTE_SEGMENTS.projects,
        element: <ProjectListPage />,
      },
      {
        path: ROUTE_SEGMENTS.projectDetail,
        element: <ProjectDetailPage />,
      },
      {
        path: ROUTE_SEGMENTS.cycles,
        element: <CycleListPage />,
      },
      {
        path: ROUTE_SEGMENTS.cycleDetail,
        element: <CycleDetailPage />,
      },
      {
        // Inside the parent, so an unknown URL still renders the shell.
        path: '*',
        element: <NotFound />,
      },
    ],
  },
]
