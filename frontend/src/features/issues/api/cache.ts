import type { ApolloCache } from '@apollo/client'

import { IssueListDocument } from './documents'
import type { IssueDetailFields, IssueListVariables } from './types'

/**
 * Variables the cached list is stored under, for one workspace.
 *
 * `after: null` is the first page's variables. The field policy in
 * `src/lib/graphql/cache.ts` keys on `workspaceSlug` and not on the cursor,
 * so every page of one workspace's `issues` lives in one cache field and
 * this reads and writes *the* list rather than a page of it -- while a
 * second workspace's list is a different field entirely, which is what stops
 * an issue created here from being prepended to another tenant's list.
 */
function firstPageVariables(workspaceSlug: string): IssueListVariables {
  return { workspaceSlug, after: null }
}

/**
 * Put a just-created issue into the cached list.
 *
 * ============================================================
 * Why this is a hand-written cache update and not a refetch
 * ============================================================
 *
 * The obvious alternative -- refetch the list after creating -- is *worse
 * here*, and not for performance reasons. The field policy in
 * `src/lib/graphql/cache.ts` treats a request with no cursor as a request for
 * the start of the list and **replaces** rather than extends:
 *
 *     if (after === null || after === undefined) return incoming
 *
 * That branch is correct and necessary (it is what stops a re-fetched page
 * one from being appended to itself). But it means a `refetch()` of the list
 * query, whose variables are `after: null`, discards every page the user
 * loaded past the first. A user who clicked "Load more" four times and then
 * created an issue would watch 100 rows collapse to 25. That is a visible
 * regression caused by the refetch, so "just refetch, it is more honest" does
 * not hold on this cache.
 *
 * ============================================================
 * Why prepending is *correct*, not merely convenient
 * ============================================================
 *
 * The list is ordered `created_at DESC, id DESC`
 * (`app/repositories/issues.py`), so a row created now sorts ahead of every
 * row already fetched. Head is where the server would put it.
 *
 * The part worth stating explicitly is that this leaves `pageInfo` alone, and
 * that is right *because the pagination is keyset and not offset*. `endCursor`
 * encodes the position of the last row fetched -- the tail frontier -- and
 * inserting a row at the head does not move the tail. The next "Load more"
 * asks for rows before that same cursor and gets exactly what it would have
 * got. Under OFFSET pagination the same insertion would shift every following
 * page by one and duplicate a row at each boundary; that bug does not exist
 * here, and the reason it does not is the cursor.
 *
 * ============================================================
 * Why the merge policy's dedupe is not being fought
 * ============================================================
 *
 * `cache.updateQuery` reads the whole accumulated list, hands it here, and
 * writes the result back *through* the same merge function -- it does not go
 * around it, the way `cache.modify` would. On that write `args.after` is
 * null, so the merge takes the reset branch and the field becomes exactly
 * what is returned below: every page already loaded, plus the new row at the
 * head. Nothing is appended, so nothing can be duplicated, and the policy's
 * dedupe is never asked to clean up after this function.
 *
 * The identity check below is therefore not covering the merge policy. It
 * covers this function running twice for one issue -- a retried update, a
 * future optimistic response reconciled against the real one -- and it is
 * three lines, so it is here rather than argued about.
 *
 * ============================================================
 * When this does nothing, on purpose
 * ============================================================
 *
 * If the list has never been read, `readQuery` returns null and the update is
 * skipped: there is no list on screen to keep consistent, and writing one
 * would invent a first page out of a single row and then report
 * `hasNextPage: false` about it. The next mount of the list fetches normally.
 */
export function prependCreatedIssue(
  cache: ApolloCache,
  created: IssueDetailFields,
  workspaceSlug: string,
): void {
  cache.updateQuery(
    { query: IssueListDocument, variables: firstPageVariables(workspaceSlug) },
    (existing) => {
      if (existing === null) {
        return
      }

      if (existing.issues.nodes.some((node) => node.id === created.id)) {
        return
      }

      return {
        ...existing,
        issues: {
          ...existing.issues,
          // `created` carries `description` as well, which the list document
          // does not select. `writeQuery` writes the document's selection and
          // ignores the rest, so the extra field is harmless -- and it is
          // already in the cache from the mutation's own normalised result,
          // which is what makes opening the new issue's detail view instant.
          nodes: [created, ...existing.issues.nodes],
        },
      }
    },
  )
}
