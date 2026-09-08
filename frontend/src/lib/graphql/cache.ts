import { InMemoryCache } from '@apollo/client'
import type { FieldPolicy, Reference } from '@apollo/client'

/**
 * The cached shape every connection in this schema shares.
 *
 * `nodes` holds `Reference`s, not entity objects: every node type here
 * exposes an `id`, so Apollo normalises each one into its own cache entry
 * (`Issue:<uuid>`, `Comment:<uuid>`) and leaves a pointer behind. That is why
 * the merge below can append two pages without worrying about the fields each
 * page selected -- it is moving pointers, and the entities they point at are
 * merged field-wise elsewhere.
 *
 * Deliberately minimal. Operation result types belong with the features that
 * run the operations; this is only what the field policy itself must know.
 */
export interface CursorConnectionValue {
  nodes: readonly Reference[]
  pageInfo: {
    hasNextPage: boolean
    endCursor: string | null
  }
}

/** The name this was introduced under, kept for callers that use it. */
export type IssueConnectionValue = CursorConnectionValue

/**
 * How pages of one cursor-paginated connection accumulate in the cache.
 *
 * The backend's connections are `{ nodes, pageInfo }` -- not Relay's
 * `edges`/`node`, and with no per-node cursor -- so none of Apollo's
 * prebuilt helpers (`relayStylePagination`, `offsetLimitPagination`) fits.
 * This is written out instead, once, and applied to every such field: the
 * two on `Query` and the three hanging off `Issue`. They are the same shape,
 * paginated the same way, with the same `after: null` means "start the list
 * over" contract, so they share a merge rather than getting five copies of
 * one that can drift apart.
 *
 * ## Why `keyArgs` is a parameter and the merge is not
 *
 * `keyArgs` answers one question: which arguments mean "a *different* list"?
 * That is a fact about the field, and the only part that genuinely differs
 * between them, so it is what the caller supplies.
 *
 * The default (no `keyArgs`) would key on *all* arguments, and that is the
 * failure this policy exists to prevent. `comments(first: 20)` and
 * `comments(first: 20, after: "abc")` would become two unrelated cache
 * entries; `fetchMore` would write page two into a field that no active
 * query is watching, and the thread on screen would never grow no matter how
 * many times the user clicked. Apollo does not report that as an error.
 *
 * ## Why a key list, and not `false`
 *
 * `keyArgs: false` -- key on the field name alone -- is right only for a
 * field whose every argument describes *where in* one list a page sits.
 * `issues` and `labels` also take `workspaceSlug`, which DOES select a
 * different list. Left as `false`, pages from two workspaces would merge into
 * one cache field and the screen would show another tenant's rows -- a
 * rendering of a cross-tenant leak that the server correctly refused to
 * produce.
 *
 * Keys are named positively rather than as an exclusion of `first`/`after`,
 * so the next argument someone adds defaults to NOT splitting the list, and
 * has to be considered here before it can. An argument that describes where
 * in one list a page sits stays out of the array; one that describes which
 * list is being read goes in.
 *
 * The connections on `Issue` pass `false`: they are already scoped by the
 * entity they hang from (`Issue:<uuid>.comments`), so the only arguments
 * they take are positional ones and there is no tenancy question left for
 * `keyArgs` to answer.
 */
function cursorConnectionPolicy(
  keyArgs: FieldPolicy<CursorConnectionValue>['keyArgs'],
): FieldPolicy<CursorConnectionValue> {
  return {
    keyArgs,

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
          /*
           * `filter` and `orderBy` are key arguments, not merge arguments.
           * Each names a DIFFERENT list -- "Ana's issues by due date" is not
           * a page of "everything, newest first" -- so merging their pages
           * under one key would interleave two orderings into a sequence
           * neither query asked for, and `fetchMore` would resume one list
           * with the other's cursor. Apollo serialises the whole object, so
           * two filters that differ in any field key separately.
           */
          issues: cursorConnectionPolicy(['workspaceSlug', 'filter', 'orderBy']),
          labels: cursorConnectionPolicy(['workspaceSlug']),

          /*
           * `projects` is the same connection, paged the same way, and it
           * needs the same merge for the same reason: without a policy,
           * `fetchMore` writes page two under a different cache key (the
           * default keys on every argument, `after` included), no active
           * query watches that key, and "Load more" becomes a button that
           * sends a request and changes nothing. Apollo does not report it.
           *
           * The key list differs from `issues` in one way, and it is the
           * schema talking: there is no `teamId` argument, because a project
           * spans teams (`Project.teamIds` is a list). The workspace is the
           * only argument that selects a different list.
           */
          projects: cursorConnectionPolicy(['workspaceSlug']),

          /*
           * `notifications` is the same connection again, and needs the same
           * merge for the same reason.
           *
           * Its key list carries one argument the others do not:
           * `unreadOnly` genuinely selects a *different list*, so the inbox's
           * filter toggle reads its own cache entry rather than merging an
           * unread page into the list of everything. Left out, switching the
           * toggle would append one list to the other and the read rows would
           * never leave the unread view.
           *
           * `first` and `after` stay out, as everywhere: they say where in
           * one list a page sits, not which list is being read.
           */
          notifications: cursorConnectionPolicy(['workspaceSlug', 'unreadOnly']),

          /*
           * `initiatives` is `projects` again in every respect that matters
           * here: a connection of entities carrying an `id`, keyset-paged,
           * whose only list-selecting argument is the workspace. Both the
           * initiatives screen and the roadmap read it, and both want page
           * two to arrive rather than vanish.
           */
          initiatives: cursorConnectionPolicy(['workspaceSlug']),

          /*
           * `documents` takes two optional narrowings beyond the workspace,
           * and both are in the key list because both genuinely select a
           * DIFFERENT list: the documents of one project are not a page of
           * the documents of the workspace. Left out, opening a project's
           * documents would append them to the workspace list and they would
           * never leave it again.
           */
          documents: cursorConnectionPolicy([
            'workspaceSlug',
            'projectId',
            'initiativeId',
          ]),
        },
      },
      Issue: {
        fields: {
          comments: cursorConnectionPolicy(false),
          children: cursorConnectionPolicy(false),
          relations: cursorConnectionPolicy(false),
        },
      },
      Document: {
        fields: {
          /*
           * `false` for the reason the `Issue` connections use it: these hang
           * off `Document:<uuid>` and are already scoped by it, so the only
           * arguments they take are positional.
           *
           * Neither is paginated by the screen today -- one page of each is
           * what the detail document asks for. The policy is still needed,
           * because `documentEdit`, `documentRestore` and the two comment
           * mutations all write a fresh `Document` into the cache, and
           * without a merge for these two fields Apollo replaces the stored
           * connection object wholesale and warns that cache data may be
           * lost. The merge below makes that write a merge instead.
           */
          revisions: cursorConnectionPolicy(false),
          comments: cursorConnectionPolicy(false),
        },
      },
    },
  })
}
