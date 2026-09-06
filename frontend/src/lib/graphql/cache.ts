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
 * ## When `keyArgs: false` becomes wrong
 *
 * It is correct only while no argument to `issues` selects a different list.
 * That holds today -- the field takes `first` and `after` and nothing else.
 * It stops holding the moment the field gains a scoping or filtering
 * argument, which backend Phase 1b-5 (workspace tenancy) is expected to add.
 * With `keyArgs: false` still in place, pages from two different workspaces
 * would merge into one list and the UI would show another tenant's issues.
 *
 * So: whoever adds `workspaceId`, `filter` or `orderBy` to this query must
 * change this to an explicit allow-list at the same time, e.g.
 * `keyArgs: ['workspaceId']`. Naming the args positively rather than
 * excluding `first`/`after` keeps a newly added argument from defaulting
 * into the key and silently splitting the list again.
 */
const issuesFieldPolicy: FieldPolicy<IssueConnectionValue> = {
  keyArgs: false,

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
        },
      },
    },
  })
}
