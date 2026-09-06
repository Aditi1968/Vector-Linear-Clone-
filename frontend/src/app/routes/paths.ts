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
 * Nothing here invents a workspace concept. There is no workspace GraphQL
 * surface yet, no slug, no route param, and this module does not pretend
 * otherwise. What it does is put the *seam* in one file, so that the concept
 * can be added in one place when the backend actually has one.
 */

/**
 * The single spelling of the issues segment. Both the route table and the
 * path builders derive from it, so the router and the links it renders
 * cannot drift apart.
 */
const ISSUES_SEGMENT = 'issues'

/** The dynamic segment carrying an issue id, as `useParams` will key it. */
export const ISSUE_ID_PARAM = 'issueId'

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
} as const

/** Every URL a component is allowed to navigate to. */
export interface AppPaths {
  /** The issue list. */
  issues: () => string
  /** One issue's detail view. */
  issue: (issueId: string) => string
}

/**
 * Build the path set for a given scope prefix.
 *
 * `scopePrefix` is the seam. It is `''` today, which yields the flat paths
 * the router currently declares. When workspaces land it becomes
 * `/${workspaceSlug}` and every path gains the segment at once -- with no
 * change to any caller, because callers only ever ask for `issues()` or
 * `issue(id)` and never see the prefix.
 */
export function createAppPaths(scopePrefix = ''): AppPaths {
  return {
    issues: () => `${scopePrefix}/${ISSUES_SEGMENT}`,

    // Encoded because an id becomes a path segment. Ids are UUIDs today and
    // encoding one is a no-op, but the builder is the right place for the
    // rule, and it will not be revisited when ids stop being UUIDs.
    issue: (issueId: string) =>
      `${scopePrefix}/${ISSUES_SEGMENT}/${encodeURIComponent(issueId)}`,
  }
}

/**
 * The unscoped path set.
 *
 * For code that runs outside React and therefore cannot use a hook -- the
 * route table's redirect, for instance. Components use `useAppPaths()`,
 * which is the form that survives the addition of a workspace scope.
 */
export const appPaths: AppPaths = createAppPaths()
