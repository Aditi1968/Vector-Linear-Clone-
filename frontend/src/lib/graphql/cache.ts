import { InMemoryCache } from '@apollo/client'
import type { FieldPolicy, Reference } from '@apollo/client'

/**
 * The cached shape of `IssueConnection`.
 *
 * `nodes` holds `Reference`s, not `Issue` objects: `Issue` exposes an `id`,
 * so Apollo normalises each one into its own cache entry (`Issue:<uuid>`)
 * and leaves a pointer behind. That is why the merge below can append two
 * pages without worrying about the fields each page selected -- it is moving
 * pointers, and the entities they point at are merged field-wise elsewhere.
 *
 * Deliberately minimal. Operation result types belong with the features that
 * run the operations; this is only what the field policy itself must know.
 */
export interface IssueConnectionValue {
  nodes: readonly Reference[]
  pageInfo: {
    hasNextPage: boolean
    endCursor: string | null
  }
}

/**
 * How pages of `issues` accumulate in the cache.
 *
 * The backend's connection is `{ nodes, pageInfo }` -- not Relay's
 * `edges`/`node`, and with no per-node cursor -- so none of Apollo's
 * prebuilt helpers (`relayStylePagination`, `offsetLimitPagination`) fits.
 * This is written out instead.
 *
 * ## Why `keyArgs: false`
 *
 * `keyArgs` answers one question: which arguments mean "a *different* list"?
 * `issues` takes `first` and `after`, and neither does. Both describe *where
 * in* a single list a page sits. So every `issues(...)` call refers to the
 * same logical list, and all of them should land in one cache field --
 * which is exactly what `keyArgs: false` means: key on the field name alone,
 * ignore the arguments entirely.
 *
 * The default (no `keyArgs`) would key on *all* arguments, and that is the
 * failure this policy exists to prevent. `issues(first: 25)` and
 * `issues(first: 25, after: "abc")` would become two unrelated cache
 * entries; `fetchMore` would write page two into a field that no active
 * query is watching, and the list on screen would never grow no matter how
 * many times the user clicked. Apollo does not report that as an error.
 *
 * ## Why the key is a list and not `false`
 *
 * `keyArgs: false` was correct while the field took only `first` and
 * `after`. It is not correct now: `issues` takes `workspaceSlug` and
 * `teamId`, and each of those DOES select a different list. Left as `false`,
 * pages from two workspaces would merge into one cache field and the screen
 * would show another tenant's issues -- a rendering of a cross-tenant leak
 * that the server correctly refused to produce.
 *
 * Named positively rather than as an exclusion of `first`/`after`, so the
 * next argument someone adds defaults to NOT splitting the list, and has to
 * be considered here before it can. An argument that describes where in one
 * list a page sits stays out of this array; one that describes which list is
 * being read goes in.
 */
const issuesFieldPolicy: FieldPolicy<IssueConnectionValue> = {
  keyArgs: ['workspaceSlug', 'teamId'],

  merge(existing, incoming, { args, readField }) {
    // `args` is typed as `Record<string, any> | null`; routing it through
    // `unknown` keeps that `any` from spreading into the logic below.
    const after: unknown = args?.['after']

    // A request with no cursor is a request for the *start* of the list, so
    // it replaces rather than extends. Without this branch, every refetch of
    // page one -- an explicit `refetch()`, a `cache-and-network` poll, a
    // StrictMode double-mount in development -- would append a second copy
    // of the rows already on screen. That is the "load more duplicates
    // rows" bug, and it shows up as a UI fault long after the cause.
    if (after === null || after === undefined || existing === undefined) {
      return incoming
    }

    const nodes: Reference[] = []
    const seen = new Set<string>()

    for (const node of [...existing.nodes, ...incoming.nodes]) {
      // Identity comes from the entity, not from array position. Keyset
      // pagination does not return overlapping rows, so in normal operation
      // this drops nothing; it is here for the case where the same
      // `fetchMore` is dispatched twice (a double click, a double-invoked
      // effect) and the same cursor is fetched twice.
      const id = readField<string>('id', node)
      const key = id ?? node.__ref

      if (seen.has(key)) {
        continue
      }

      seen.add(key)
      nodes.push(node)
    }

    return {
      ...incoming,
      // `pageInfo` is taken from `incoming`, never merged: `hasNextPage` and
      // `endCursor` describe the frontier of the list, and the frontier is
      // wherever the newest page ended. Keeping the older `pageInfo` would
      // hand the next `fetchMore` a cursor it has already consumed and loop
      // on the same page forever.
      nodes,
    }
  },
}

/**
 * A cache configured for this schema.
 *
 * A factory rather than a shared instance: a cache is mutable state, and two
 * tests sharing one would leak results into each other. Application code
 * gets its single cache from `createApolloClient()`.
 */
export function createCache(): InMemoryCache {
  return new InMemoryCache({
    typePolicies: {
      Query: {
        fields: {
          issues: issuesFieldPolicy,

          /*
           * `projects` is the same connection, paged the same way.
           *
           * `{ nodes, pageInfo }`, an opaque keyset cursor in `after`, no
           * per-node cursor -- so it needs the same merge, and needs it for
           * the same reason: without a policy, `fetchMore` writes page two
           * under a *different* cache key (the default keys on every
           * argument, `after` included), no active query is watching that
           * key, and "Load more" becomes a button that sends a request and
           * changes nothing on screen. Apollo does not report that.
           *
           * The key differs from `issues` in one way, and it is the schema
           * talking: there is no `teamId` argument, because a project spans
           * teams (`Project.teamIds` is a list). The workspace is the only
           * argument that selects a different list.
           */
          projects: { ...issuesFieldPolicy, keyArgs: ['workspaceSlug'] },
        },
      },
    },
  })
}
