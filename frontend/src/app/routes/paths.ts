/**
 * The one place that knows what a Vector URL looks like.
 *
 * No component may write a route as a string literal. That rule is what made
 * the workspace segment a change to this file instead of a sweep: `/issues`
 * became `/:workspaceSlug/issues` here, and every link in the product
 * followed without being edited.
 *
 * ## Two shapes of the same set
 *
 * `paths` takes the workspace slug as its first argument -- `paths.issue(
 * slug, id)` -- and is the form to reach for when the workspace is *not* the
 * one in the address bar: the switcher links into other workspaces, and the
 * entry screen resolves one before there is a URL to read.
 *
 * `createAppPaths(slug)` is the same set with the slug already applied, and
 * `useAppPaths()` returns it bound to the slug in the current URL. That is
 * what screens use. It is derived from `paths` by binding rather than
 * re-declared, so the two cannot drift.
 *
 * ## The slug is a route parameter, never a claim
 *
 * Nothing here checks anything. A slug in the URL is a string the user typed;
 * it says nothing about membership, and the server answers NOT_FOUND
 * identically for a workspace that does not exist and one the caller may not
 * see. `AppLayout` is where a slug becomes a workspace, and it does it by
 * looking the slug up in `myWorkspaces` -- so an address for a workspace the
 * viewer is not in is never asked about at all.
 */

/**
 * The dynamic segments, as `useParams` keys them.
 *
 * Named here rather than written into the route table and the hooks
 * separately: the two have to agree exactly, and a typo in either is not a
 * crash but an `undefined` param that turns every request into a failure a
 * long way from its cause.
 */
export const WORKSPACE_SLUG_PARAM = 'workspaceSlug'
export const ISSUE_ID_PARAM = 'issueId'
export const TEAM_KEY_PARAM = 'teamKey'
export const PROJECT_ID_PARAM = 'projectId'
export const CYCLE_ID_PARAM = 'cycleId'

/**
 * Where a signed-in user with no workspace belongs.
 *
 * Unscoped, because it is the one authenticated screen outside a workspace --
 * it is how you get your first one. The route itself belongs to
 * `features/onboarding`; this is the spelling the shell redirects to.
 */
export const ONBOARDING_PATH = '/onboarding'

/**
 * The routes a signed-out visitor may reach, spelled once.
 *
 * Absolute where `ROUTE_SEGMENTS` is relative, and deliberately kept out of
 * `paths` below rather than added to it. Everything in `paths` takes a
 * workspace slug because everything in `paths` lives inside a workspace;
 * signing in is what *produces* a workspace, so a sign-in URL built from a
 * workspace scope would be reachable only by people who no longer need it.
 * Separating them means a component cannot accidentally scope a public page,
 * and `useAppPaths()` cannot offer one.
 */
export const PUBLIC_SEGMENTS = {
  login: 'login',
  register: 'register',
} as const

/** The three URLs that exist without a session. */
export const publicPaths = {
  /** The public marketing page at the root. */
  landing: () => '/',

  /** Sign in. */
  login: () => `/${PUBLIC_SEGMENTS.login}`,

  /** Create an account. */
  register: () => `/${PUBLIC_SEGMENTS.register}`,
} as const

/**
 * Route paths as the router declares them, relative to `/:workspaceSlug`.
 *
 * Relative, not absolute, and that is load-bearing: they are declared as
 * children of the workspace route (see ./routes.tsx), so React Router
 * resolves them against whatever that parent's path is. The builders below
 * derive from the same spellings, so the router and the links it renders
 * cannot disagree.
 */
export const ROUTE_SEGMENTS = {
  myIssues: 'my-issues',
  inbox: 'inbox',
  issues: 'issues',
  issueDetail: `issues/:${ISSUE_ID_PARAM}`,
  board: 'board',
  projects: 'projects',
  projectDetail: `projects/:${PROJECT_ID_PARAM}`,
  team: `team/:${TEAM_KEY_PARAM}`,
  teamIssues: `team/:${TEAM_KEY_PARAM}/issues`,
  cycles: 'cycles',
  cycleDetail: `cycles/:${CYCLE_ID_PARAM}`,
  members: 'members',
  settings: 'settings',
  search: 'search',

  /*
    The second wave of screens. Each is a real URL with a placeholder behind
    it (see ./routes.tsx) rather than a segment reserved for later: a route
    that exists is a mount point whoever builds the screen can find, and a
    link that already resolves is one the rest of the product can render
    today without knowing whether the page is finished.

    All flat and all workspace-scoped. None takes a parameter yet; a detail
    view arrives as its own segment beside the list, the way `issueDetail`
    sits beside `issues`.
  */
  triage: 'triage',
  savedViews: 'saved-views',
  favorites: 'favorites',
  initiatives: 'initiatives',
  roadmap: 'roadmap',
  documents: 'documents',
  templates: 'templates',
  releases: 'releases',
  environments: 'environments',
  labelGroups: 'label-groups',
  analytics: 'analytics',
  semanticSearch: 'semantic-search',
} as const

/**
 * One path segment.
 *
 * Encoded because every value below comes from somewhere the router does not
 * control -- a slug the user chose, an id from the server, a team key.
 * Encoding a UUID is a no-op today; the builder is still the right place for
 * the rule, and it will not be revisited when ids stop being UUIDs.
 */
function seg(value: string): string {
  return encodeURIComponent(value)
}

/**
 * Every URL a component is allowed to navigate to, as a function of the
 * workspace it is in.
 *
 * The workspace root is not a page -- ./routes.tsx renders the issue list
 * there -- but it is still a builder, because "back to this workspace" is a
 * real destination and what sits at it is the shell's decision to change.
 */
export const paths = {
  /** The workspace's front door. */
  workspace: (slug: string) => `/${seg(slug)}`,

  /** Issues assigned to the viewer, across teams. */
  myIssues: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.myIssues}`,

  /** The viewer's notifications. */
  inbox: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.inbox}`,

  /** Every issue in the workspace. */
  issues: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.issues}`,

  /** One issue's detail view. */
  issue: (slug: string, issueId: string) =>
    `/${seg(slug)}/${ROUTE_SEGMENTS.issues}/${seg(issueId)}`,

  /**
   * The board.
   *
   * Takes no team, although a board is always one team's: workflow states
   * belong to a team, so there is no workspace-wide board to draw. The team
   * rides in the query string (`?team=ENG`) alongside the filters, the sort
   * and the grouping, because those are one view and one thing to link to --
   * and because a builder that took a team would produce an address the
   * sidebar could not offer without choosing a team on the user's behalf.
   */
  board: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.board}`,

  /** One team's overview. `teamKey` is the ENG in ENG-42. */
  team: (slug: string, teamKey: string) => `/${seg(slug)}/team/${seg(teamKey)}`,

  /** One team's issues. */
  teamIssues: (slug: string, teamKey: string) =>
    `/${seg(slug)}/team/${seg(teamKey)}/issues`,

  /**
   * The workspace's cycles, with the team chosen on the screen.
   *
   * Not `team/:teamKey/cycles`, though cycles ARE team-scoped in the API.
   * `Cycle` exposes no `teamId`, so a cycle detail page cannot build a URL
   * that names its own team -- "back to cycles" would be unbuildable from
   * the one screen that most needs it. The team is a picker instead.
   */
  cycles: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.cycles}`,

  /** One cycle, by id. */
  cycle: (slug: string, cycleId: string) =>
    `/${seg(slug)}/${ROUTE_SEGMENTS.cycles}/${seg(cycleId)}`,

  /** Every project in the workspace. */
  projects: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.projects}`,

  /** One project, by id. */
  project: (slug: string, projectId: string) =>
    `/${seg(slug)}/${ROUTE_SEGMENTS.projects}/${seg(projectId)}`,

  /** Everyone in the workspace. */
  members: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.members}`,

  /** Workspace settings. */
  settings: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.settings}`,

  /** Search within the workspace. */
  search: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.search}`,

  /* ---------------------------------------------------------------- */
  /* The second wave. Screens behind these are placeholders today; the  */
  /* addresses are final, so a link built here does not change when the */
  /* page behind it lands.                                              */
  /* ---------------------------------------------------------------- */

  /** Unassigned, unsorted work waiting to be accepted or declined. */
  triage: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.triage}`,

  /** Filter sets someone saved and named. */
  savedViews: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.savedViews}`,

  /** What the viewer starred. */
  favorites: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.favorites}`,

  /** Projects grouped into something larger than a project. */
  initiatives: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.initiatives}`,

  /** Projects and initiatives against a calendar. */
  roadmap: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.roadmap}`,

  /** Long-form writing that is not an issue. */
  documents: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.documents}`,

  /** Pre-filled issues, so a recurring piece of work is filed the same way. */
  templates: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.templates}`,

  /** What shipped, and what is in the next one. */
  releases: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.releases}`,

  /** Where the product runs, and what is deployed to each. */
  environments: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.environments}`,

  /** Labels that are mutually exclusive, administered together. */
  labelGroups: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.labelGroups}`,

  /** Throughput, cycle time, and the rest of the numbers. */
  analytics: (slug: string) => `/${seg(slug)}/${ROUTE_SEGMENTS.analytics}`,

  /** Search by meaning rather than by substring. */
  semanticSearch: (slug: string) =>
    `/${seg(slug)}/${ROUTE_SEGMENTS.semanticSearch}`,
} as const

/** The slug-first path set. */
export type WorkspaceScopedPaths = typeof paths

/**
 * The same set with the workspace already applied.
 *
 * Derived from `paths` rather than declared beside it, so a builder added
 * above appears here automatically and one removed stops compiling at its
 * call sites.
 */
export type AppPaths = {
  [Name in keyof WorkspaceScopedPaths]: WorkspaceScopedPaths[Name] extends (
    slug: string,
    ...rest: infer Rest
  ) => string
    ? (...rest: Rest) => string
    : never
}

/**
 * Bind every builder to one workspace.
 *
 * Two type moves, and both are contained. `builders` widens the object to a
 * uniform signature, because `Object.entries` would otherwise hand back a
 * union of the individual builders that no spread call can satisfy -- even
 * though every member really does take `(slug, ...string[])`. The result is
 * then asserted back to `AppPaths`, which `Object.fromEntries` cannot infer.
 * Neither assertion can drift from the truth silently: ./paths.test.ts builds
 * every path both ways and asserts they agree.
 */
export function createAppPaths(workspaceSlug: string): AppPaths {
  const builders: Record<string, (...args: string[]) => string> = paths

  const entries = Object.entries(builders).map(([name, build]) => [
    name,
    (...rest: string[]) => build(workspaceSlug, ...rest),
  ])

  return Object.fromEntries(entries) as AppPaths
}
