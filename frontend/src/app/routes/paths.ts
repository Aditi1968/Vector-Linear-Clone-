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

/** The dynamic segment carrying an issue id, as `useParams` will key it. */
export const ISSUE_ID_PARAM = 'issueId'

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
} as const

/**
 * The routes a signed-out visitor may reach, spelled once.
 *
 * Declared apart from `ROUTE_SEGMENTS` because they are absolute where those
 * are relative, and that difference is the point: signing in is what produces
 * a workspace scope, so a sign-in page underneath one would be reachable only
 * by people who no longer need it.
 */
export const PUBLIC_SEGMENTS = {
  login: 'login',
  register: 'register',
} as const

/** Every URL a component is allowed to navigate to. */
export interface AppPaths {
  /** The issue list. */
  issues: () => string
  /** One issue's detail view. */
  issue: (issueId: string) => string
  /** The public marketing page at the root. */
  landing: () => string
  /** Sign in. */
  login: () => string
  /** Create an account. */
  register: () => string
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

    // `scopePrefix` is deliberately not applied to the three below. They are
    // the URLs a visitor with no session can reach, and a visitor with no
    // session has no workspace to be scoped by.
    landing: () => '/',
    login: () => `/${PUBLIC_SEGMENTS.login}`,
    register: () => `/${PUBLIC_SEGMENTS.register}`,
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
