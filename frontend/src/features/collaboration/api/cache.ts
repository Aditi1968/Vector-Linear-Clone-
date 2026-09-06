import type { ApolloCache } from '@apollo/client'

import {
  IssueCommentsDocument,
  IssueRelationsDocument,
  IssueSubIssuesDocument,
  WorkspaceLabelsDocument,
} from './documents'
import type { Comment, IssueRelation, IssueSummary, Label } from './types'

/**
 * Keeping the cache honest after a mutation that does not return enough to
 * do it automatically.
 *
 * ============================================================
 * What is NOT in this file, and why that is the point
 * ============================================================
 *
 * Three of this feature's seven mutations need nothing here:
 *
 *   issueLabelAttach / issueLabelDetach  return the whole `Issue` with its
 *   issueSetParent (on the issue itself) `labels` / `parent` selected
 *
 * Apollo normalises those onto the same `Issue:<uuid>` every panel reads, so
 * the write is the server's own answer and every component watching that
 * issue re-renders from it. Adding a hand-written update for them would be a
 * second source of truth competing with the first, and the two would
 * eventually disagree.
 *
 * What is left here is the mutations whose payload carries a *fragment* of
 * the change -- a comment, a relation, an id -- with no path from it to the
 * connection the panel is watching. Those connections are cache fields that
 * nothing in the response mentions, so nothing updates them but this.
 *
 * ============================================================
 * Why `updateQuery` and not `refetch`
 * ============================================================
 *
 * The same reason `features/issues/api/cache.ts` gives, and it applies with
 * more force here. The merge policy in `src/lib/graphql/cache.ts` treats a
 * request with no cursor as a request for the start of the list and
 * *replaces* rather than extends. A `refetch()` of one of these panel
 * queries sends `after: null`, so it would discard every page the user had
 * loaded: a thread scrolled back through 60 comments would snap to the most
 * recent 20 the moment someone posted a reply.
 *
 * `updateQuery` reads the whole accumulated field, hands it to the callback,
 * and writes the result back *through* the same merge -- with `after: null`,
 * which takes the reset branch, so the field becomes exactly what is
 * returned and nothing is appended to itself. The dedupe in the merge policy
 * is never asked to clean up after this file.
 *
 * ============================================================
 * When these do nothing, on purpose
 * ============================================================
 *
 * If the panel's query has never been read, `updateQuery` finds nothing and
 * the update is skipped. There is no list on screen to keep consistent, and
 * writing one would invent a first page out of a single row and then report
 * `hasNextPage: false` about it. The next mount fetches normally.
 */

/**
 * The variables the first page of a panel's query is stored under.
 *
 * `after: null` is not decoration. The merge policy reads `args.after` to
 * decide whether a write starts the list or extends it, and every panel hook
 * passes the cursor explicitly for the same reason -- so the cache field
 * being read here is the one the panel is watching, byte for byte.
 */
function issueVariables(workspaceSlug: string, issueId: string) {
  return { workspaceSlug, issueId, after: null }
}

/** Identity comes from the entity, never from array position. */
function withoutId<T extends { id: string }>(
  nodes: readonly T[],
  id: string,
): T[] {
  return nodes.filter((node) => node.id !== id)
}

/**
 * `node` inserted at `index`, or nothing if the list already has it.
 *
 * The identity check is not covering the merge policy -- see the header --
 * it covers this function running twice for one entity: a retried update, a
 * future optimistic response reconciled against the real one.
 */
function insertAt<T extends { id: string }>(
  nodes: readonly T[],
  node: T,
  index: number,
): T[] | undefined {
  if (nodes.some((existing) => existing.id === node.id)) {
    return undefined
  }

  return [...nodes.slice(0, index), node, ...nodes.slice(index)]
}

/**
 * A just-posted comment, at the end of the thread.
 *
 * The end and not the beginning, because `app/repositories/comments.py`
 * orders comments `created_at, id` ASCENDING -- oldest first -- so a comment
 * written now sorts after every one already fetched. That is also why this
 * leaves `pageInfo` alone even though it is inserting at the tail: `endCursor`
 * is a *keyset* position, not an offset, so the next "load more" asks for
 * rows after the same server-issued cursor and gets exactly what it would
 * have got. Under OFFSET pagination this insertion would shift every
 * following page by one; that bug does not exist here, and the reason is the
 * cursor.
 */
export function appendComment(
  cache: ApolloCache,
  workspaceSlug: string,
  issueId: string,
  comment: Comment,
): void {
  cache.updateQuery(
    {
      query: IssueCommentsDocument,
      variables: issueVariables(workspaceSlug, issueId),
    },
    (existing) => {
      if (existing?.issue == null) {
        return
      }

      const { comments } = existing.issue
      const nodes = insertAt(comments.nodes, comment, comments.nodes.length)

      if (nodes === undefined) {
        return
      }

      return { ...existing, issue: { ...existing.issue, comments: { ...comments, nodes } } }
    },
  )
}

/**
 * A withdrawn comment, gone from the thread.
 *
 * `commentDelete` returns `deletedCommentId` and not the comment, which is
 * the correct shape for a delete: there is no entity left to describe. The
 * `Comment:<uuid>` normalised entry is deliberately left in the cache --
 * evicting it would be tidier and would also break any other query that
 * still legitimately references it. Removing the node from this connection
 * is the whole of what the server told us happened.
 */
export function removeComment(
  cache: ApolloCache,
  workspaceSlug: string,
  issueId: string,
  commentId: string,
): void {
  cache.updateQuery(
    {
      query: IssueCommentsDocument,
      variables: issueVariables(workspaceSlug, issueId),
    },
    (existing) => {
      if (existing?.issue == null) {
        return
      }

      const { comments } = existing.issue

      return {
        ...existing,
        issue: {
          ...existing.issue,
          comments: { ...comments, nodes: withoutId(comments.nodes, commentId) },
        },
      }
    },
  )
}

/**
 * A new relation, at the head.
 *
 * `app/repositories/relations.py` orders relations by the *edge's*
 * `created_at DESC` -- newest first -- so an edge created now sorts ahead of
 * every one already fetched. Head is where the server would put it.
 */
export function prependRelation(
  cache: ApolloCache,
  workspaceSlug: string,
  issueId: string,
  relation: IssueRelation,
): void {
  cache.updateQuery(
    {
      query: IssueRelationsDocument,
      variables: issueVariables(workspaceSlug, issueId),
    },
    (existing) => {
      if (existing?.issue == null) {
        return
      }

      const { relations } = existing.issue
      const nodes = insertAt(relations.nodes, relation, 0)

      if (nodes === undefined) {
        return
      }

      return { ...existing, issue: { ...existing.issue, relations: { ...relations, nodes } } }
    },
  )
}

export function removeRelation(
  cache: ApolloCache,
  workspaceSlug: string,
  issueId: string,
  relationId: string,
): void {
  cache.updateQuery(
    {
      query: IssueRelationsDocument,
      variables: issueVariables(workspaceSlug, issueId),
    },
    (existing) => {
      if (existing?.issue == null) {
        return
      }

      const { relations } = existing.issue

      return {
        ...existing,
        issue: {
          ...existing.issue,
          relations: { ...relations, nodes: withoutId(relations.nodes, relationId) },
        },
      }
    },
  )
}

/**
 * A newly adopted sub-issue, in the position the server sorts it into.
 *
 * This is the one insertion here that is not simply "an end". `children` is
 * ordered by the CHILD ISSUE's `created_at DESC`, not by when it was adopted
 * (`app/repositories/relations.py`), so an issue filed last year and made a
 * sub-issue a second ago belongs in the middle of the list, not at the top.
 * Putting it at the head would look right until the next reload moved it,
 * which is the kind of disagreement between cache and server this feature is
 * supposed to make impossible.
 *
 * ponytail: if the child is older than every row loaded so far it lands at
 * the tail of the loaded pages rather than on the unfetched page it really
 * belongs to, so it shows up earlier in the list than it eventually will.
 * That is a position being approximate, never a relationship being invented,
 * and it self-corrects on the next full read. Fixing it properly means
 * comparing against `endCursor`, which is opaque by design.
 */
export function insertChild(
  cache: ApolloCache,
  workspaceSlug: string,
  issueId: string,
  child: IssueSummary,
): void {
  cache.updateQuery(
    {
      query: IssueSubIssuesDocument,
      variables: issueVariables(workspaceSlug, issueId),
    },
    (existing) => {
      if (existing?.issue == null) {
        return
      }

      const { children } = existing.issue
      const at = children.nodes.findIndex((node) => node.createdAt < child.createdAt)
      const nodes = insertAt(
        children.nodes,
        child,
        at === -1 ? children.nodes.length : at,
      )

      if (nodes === undefined) {
        return
      }

      return { ...existing, issue: { ...existing.issue, children: { ...children, nodes } } }
    },
  )
}

export function removeChild(
  cache: ApolloCache,
  workspaceSlug: string,
  issueId: string,
  childId: string,
): void {
  cache.updateQuery(
    {
      query: IssueSubIssuesDocument,
      variables: issueVariables(workspaceSlug, issueId),
    },
    (existing) => {
      if (existing?.issue == null) {
        return
      }

      const { children } = existing.issue

      return {
        ...existing,
        issue: {
          ...existing.issue,
          children: { ...children, nodes: withoutId(children.nodes, childId) },
        },
      }
    },
  )
}

/**
 * A label just created, into the workspace's picker list.
 *
 * `app/repositories/labels.py` orders labels `name, id`, so this is a sorted
 * insert and not an append: the picker is a list someone reads down looking
 * for a name, and a new label appearing at the bottom until the next reload
 * moved it is exactly the sort of small dishonesty that makes people stop
 * trusting a list.
 *
 * Keyed on `workspaceSlug` through the query variables, which is what stops a
 * label created here from being written into another tenant's cached list --
 * the field policy keys on the same argument.
 */
export function insertWorkspaceLabel(
  cache: ApolloCache,
  workspaceSlug: string,
  label: Label,
): void {
  cache.updateQuery(
    { query: WorkspaceLabelsDocument, variables: { workspaceSlug, after: null } },
    (existing) => {
      if (existing === null) {
        return
      }

      const { labels } = existing
      const at = labels.nodes.findIndex((node) => node.name > label.name)
      const nodes = insertAt(
        labels.nodes,
        label,
        at === -1 ? labels.nodes.length : at,
      )

      if (nodes === undefined) {
        return
      }

      return { ...existing, labels: { ...labels, nodes } }
    },
  )
}
