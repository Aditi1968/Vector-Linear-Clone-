import type { RouteObject } from 'react-router-dom'

import { RequireAuth, authRoutes } from '../../features/auth'
import { BoardScreen } from '../../features/board'
import { CycleDetailPage, CycleListPage } from '../../features/cycles'
import { DocumentsPage } from '../../features/documents'
import { FavoritesPage } from '../../features/favorites'
import { InboxPage } from '../../features/inbox'
import { InitiativesPage } from '../../features/initiatives'
import { IssueDetailPage, IssueListPage } from '../../features/issues'
import { MembersPage } from '../../features/members'
import { MyIssuesPage } from '../../features/myIssues'
import { onboardingRoutes } from '../../features/onboarding'
import { ProjectDetailPage, ProjectListPage } from '../../features/projects'
import { RoadmapPage } from '../../features/roadmap'
import { SavedViewsPage } from '../../features/savedViews'
import { SearchPage } from '../../features/search'
import { SettingsPage } from '../../features/settings'
import { TeamIssuesPage, TeamPage } from '../../features/teams'
import { TemplatesPage } from '../../features/templates'
import { TriagePage } from '../../features/triage'
import { AppLayout } from '../layout'
import { NotFound } from './NotFound'
import { ROUTE_SEGMENTS, WORKSPACE_SLUG_PARAM } from './paths'
import { Placeholder } from './Placeholder'
import { RouteError } from './RouteError'

/**
 * The routes whose screens are still to come.
 *
 * A table rather than five near-identical entries, because every one of them
 * differs in exactly three strings and nothing else. The description is what
 * the page says it will be -- a sentence about the screen, never a promise
 * about when.
 *
 * Seven of the original twelve have landed and left this table: triage, saved
 * views, favorites and templates first, then initiatives, roadmap and
 * documents. Each is a real screen now, mounted individually below. Removing
 * an entry from here and adding a route object there is the whole of what
 * replacing a placeholder involves.
 */
const SECOND_WAVE = [
  {
    segment: ROUTE_SEGMENTS.releases,
    title: 'Releases',
    description: 'What shipped, and what is going out next.',
  },
  {
    segment: ROUTE_SEGMENTS.environments,
    title: 'Environments',
    description: 'Where the product runs, and what is deployed to each.',
  },
  {
    segment: ROUTE_SEGMENTS.labelGroups,
    title: 'Label groups',
    description: 'Labels that are mutually exclusive, administered together.',
  },
  {
    segment: ROUTE_SEGMENTS.analytics,
    title: 'Analytics',
    description: 'Throughput, cycle time, and how the workspace is actually moving.',
  },
  {
    segment: ROUTE_SEGMENTS.semanticSearch,
    title: 'Semantic search',
    description: 'Search by what an issue means rather than by the words it contains.',
  },
] as const

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
 * The five second-wave routes at the foot of the table. Each mounts
 * `./Placeholder.tsx`, which is the right thing for a route whose screen is
 * somebody else's to write: the route is real, the URL is the one `paths`
 * builds, and the page says outright that it does not exist yet rather than
 * leaving a dead link behind.
 *
 * Replacing one is a one-line change here -- swap the `element` for the real
 * page -- and nothing else in this file moves. That is the point of listing
 * them: the feature that lands owns its own directory and this entry, and no
 * two features ever edit the same lines.
 *
 * Mounted eagerly rather than through `React.lazy`, because a placeholder is
 * a heading and a sentence and there is no `<Suspense>` boundary in the shell
 * to catch the promise. Whoever replaces one with a real screen can add both
 * together.
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
        // Which team's board, and how it is filtered, sorted and grouped, are
        // all query parameters rather than segments -- one view, one link.
        path: ROUTE_SEGMENTS.board,
        element: <BoardScreen />,
      },
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
        element: <SettingsPage />,
      },
      {
        path: ROUTE_SEGMENTS.search,
        element: <SearchPage />,
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
        path: ROUTE_SEGMENTS.triage,
        element: <TriagePage />,
      },
      {
        // The list and the selected view's results are one screen: there is
        // no `savedViewDetail` segment, so selecting a view changes what the
        // panel shows rather than navigating.
        path: ROUTE_SEGMENTS.savedViews,
        element: <SavedViewsPage />,
      },
      {
        path: ROUTE_SEGMENTS.favorites,
        element: <FavoritesPage />,
      },
      {
        path: ROUTE_SEGMENTS.templates,
        element: <TemplatesPage />,
      },
      {
        // List and panel on one screen, for the reason saved views are:
        // there is no `initiatives/:id` segment to navigate to.
        path: ROUTE_SEGMENTS.initiatives,
        element: <InitiativesPage />,
      },
      {
        // A view over `initiatives` and `projects` rather than a screen with
        // a query of its own -- the schema has no roadmap field.
        path: ROUTE_SEGMENTS.roadmap,
        element: <RoadmapPage />,
      },
      {
        path: ROUTE_SEGMENTS.documents,
        element: <DocumentsPage />,
      },
      ...SECOND_WAVE.map(({ segment, title, description }) => ({
        path: segment,
        element: <Placeholder title={title} description={description} />,
      })),
      {
        // Inside the parent, so an unknown URL still renders the shell.
        path: '*',
        element: <NotFound />,
      },
    ],
  },
]
