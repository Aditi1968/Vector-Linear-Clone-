/**
 * The one place that knows what a Vector URL looks like.
 *
 * No component may write a route as a string literal. That rule is not
 * stylistic -- it is what makes backend Phase 1b-5 (workspace tenancy)
 * a small change instead of a sweep. Today `/issues` is a top-level route;
 * once workspaces are exposed it becomes `/:workspaceSlug/issues`. If path
 * strings are scattered through feature components, every one of them has to
 * be found and rewritten, and the ones that are missed keep working in
 * development (where there is one workspace) and break in production.
 *
 * The workspace segment has now arrived, and the seam is why it cost two
 * lines here and none anywhere else: `createAppPaths` already took a prefix,
 * so every path gained `/:workspaceSlug` by the prefix becoming non-empty.
 */

/**
 * The single spelling of the issues segment. Both the route table and the
 * path builders derive from it, so the router and the links it renders
 * cannot drift apart.
 */
const ISSUES_SEGMENT = 'issues'

/** As above, for the two planning surfaces. */
const PROJECTS_SEGMENT = 'projects'
const CYCLES_SEGMENT = 'cycles'

/** The dynamic segment carrying an issue id, as `useParams` will key it. */
export const ISSUE_ID_PARAM = 'issueId'

/** The same, for a project and for a cycle. */
export const PROJECT_ID_PARAM = 'projectId'
export const CYCLE_ID_PARAM = 'cycleId'

/**
 * The dynamic segment carrying the workspace slug, as `useParams` keys it.
 *
 * Named here rather than written into the route table and the hook
 * separately: the two have to agree exactly, and a typo in either is not a
 * crash but a `undefined` slug that turns every request into an
 * authentication failure a long way from its cause.
 */
export const WORKSPACE_SLUG_PARAM = 'workspaceSlug'

/**
 * Route paths as the router declares them.
 *
 * Relative, not absolute, and that is load-bearing. They are declared as
 * children of a single parent route (see ./routes.tsx), so React Router
 * resolves them against whatever that parent's path turns out to be.
 * Introducing a workspace segment means changing the parent's path to
 * `/:workspaceSlug` -- these entries do not change at all.
 */
export const ROUTE_SEGMENTS = {
  issues: ISSUES_SEGMENT,
  issueDetail: `${ISSUES_SEGMENT}/:${ISSUE_ID_PARAM}`,
  projects: PROJECTS_SEGMENT,
  projectDetail: `${PROJECTS_SEGMENT}/:${PROJECT_ID_PARAM}`,
  cycles: CYCLES_SEGMENT,
  cycleDetail: `${CYCLES_SEGMENT}/:${CYCLE_ID_PARAM}`,
} as const

/** Every URL a component is allowed to navigate to. */
export interface AppPaths {
  /** The issue list. */
  issues: () => string
  /** One issue's detail view. */
  issue: (issueId: string) => string
  /** The project list. */
  projects: () => string
  /** One project's detail view. */
  project: (projectId: string) => string
  /**
   * The cycles screen.
   *
   * No team in the URL, although `cycles(teamId:)` requires one: which team
   * a person is looking at is a control on the screen, not an address. A
   * link to `/acme/cycles` has to resolve to *something* for anyone who
   * follows it, and a slug plus a team id is a URL nobody can type.
   */
  cycles: () => string
  /** One cycle's detail view. */
  cycle: (cycleId: string) => string
}

/**
 * Build the path set for a given scope prefix.
 *
 * `scopePrefix` is the seam, and it is `/${workspaceSlug}` in the running
 * application: every path gains the segment at once, with no change to any
 * caller, because callers only ever ask for `issues()` or `issue(id)` and
 * never see the prefix. It still defaults to `''` for the one caller that
 * has no workspace -- ./WorkspaceEntry, which is resolving which one to use.
 */
export function createAppPaths(scopePrefix = ''): AppPaths {
  return {
    issues: () => `${scopePrefix}/${ISSUES_SEGMENT}`,

    // Encoded because an id becomes a path segment. Ids are UUIDs today and
    // encoding one is a no-op, but the builder is the right place for the
    // rule, and it will not be revisited when ids stop being UUIDs.
    issue: (issueId: string) =>
      `${scopePrefix}/${ISSUES_SEGMENT}/${encodeURIComponent(issueId)}`,

    projects: () => `${scopePrefix}/${PROJECTS_SEGMENT}`,
    project: (projectId: string) =>
      `${scopePrefix}/${PROJECTS_SEGMENT}/${encodeURIComponent(projectId)}`,

    cycles: () => `${scopePrefix}/${CYCLES_SEGMENT}`,
    cycle: (cycleId: string) =>
      `${scopePrefix}/${CYCLES_SEGMENT}/${encodeURIComponent(cycleId)}`,
  }
}

/**
 * The unscoped path set.
 *
 * For code that runs outside React and therefore cannot use a hook. Nothing
 * a signed-in user sees should reach for it now that every screen is under a
 * workspace: an unscoped `/issues` matches `/:workspaceSlug` with the slug
 * `issues`, which is a workspace nobody has. Components use `useAppPaths()`.
 */
export const appPaths: AppPaths = createAppPaths()
