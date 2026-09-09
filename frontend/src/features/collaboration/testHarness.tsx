import { render } from '@testing-library/react'
import type { RenderResult } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { UserEvent } from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'

import { AppProviders } from '../../app/providers/AppProviders'
import { WORKSPACE_SLUG_PARAM } from '../../app/routes'
import { createTestClient } from '../../test/client'
import { ControlledLink } from '../../test/controlledLink'
import type {
  CommentAuthorsQuery,
  CommentCreateMutation,
  CommentDeleteMutation,
  CommentFieldsFragment,
  IssueCommentsQuery,
  IssueLabelAttachMutation,
  IssueLabelsQuery,
  IssueRelationCreateMutation,
  IssueRelationsQuery,
  IssueSearchQuery,
  IssueSetParentMutation,
  IssueSubIssuesQuery,
  IssueSummaryFieldsFragment,
  LabelFieldsFragment,
  WorkspaceLabelsQuery,
} from '../../generated/operations'
import type { IssueRelationType } from './api'

/**
 * Mounting one panel against a controllable network.
 *
 * Not `src/test/render.tsx`, which boots the whole application through the
 * real route table: these are panels nothing routes to yet -- A6 is building
 * the view that mounts them, in parallel -- so there is no URL that renders
 * one. What is kept from that harness is the part that matters: the real
 * `createCache()` and the real `ControlledLink`. A test that swapped in a
 * plain `new InMemoryCache()` would still render rows and still pass a naive
 * "the comment appears" assertion, while proving nothing about the merge
 * policy or the hand-written cache updates that most of this feature is.
 *
 * A router is provided because the panels build issue links through
 * `useAppPaths()`, which reads the workspace out of the route. One catch-all
 * route under `/:workspaceSlug` is enough for that and keeps the harness from
 * depending on a route table A4 is restructuring in parallel.
 */

export const WORKSPACE_SLUG = 'acme'

/**
 * Canonical hyphenated UUIDs, because `UUID!` is a real scalar and ids from
 * these fixtures end up in mutation variables that a test asserts on.
 */
export function uuid(seed: number): string {
  return `00000000-0000-4000-8000-${String(seed).padStart(12, '0')}`
}

export const ISSUE_ID = uuid(1)
export const VIEWER_ID = uuid(900)
export const OTHER_USER_ID = uuid(901)

/**
 * Somebody who has left the workspace, and is still in `authorsData`.
 *
 * That is what the server sends, not a convenience: 026 stamps a removed
 * membership instead of deleting it and `workspaceMembers` returns the ones
 * who left, precisely so a comment they wrote still resolves to their name.
 * `CommentAuthors` selects no `removedAt` because this panel has no use for
 * it -- it names people, and a name does not expire -- so a former member is
 * shaped exactly like a current one here, which is the point.
 */
export const FORMER_USER_ID = uuid(902)

export function cursor(label: string): string {
  return `Y3Vyc29yOnsiY3JlYXRlZF9hdCI6${label}`
}

/** A fixed instant, so nothing here depends on when the suite runs. */
const BASE_TIME = Date.parse('2026-01-15T12:00:00.000Z')

function timestamp(seed: number): string {
  return new Date(BASE_TIME - seed * 3_600_000).toISOString()
}

export interface RenderPanelResult extends RenderResult {
  link: ControlledLink
  user: UserEvent
}

export function renderPanel(ui: ReactNode): RenderPanelResult {
  const link = new ControlledLink()
  const client = createTestClient(link)

  const router = createMemoryRouter(
    [{ path: `/:${WORKSPACE_SLUG_PARAM}/*`, element: ui }],
    { initialEntries: [`/${WORKSPACE_SLUG}/issues/${ISSUE_ID}`] },
  )

  const view = render(
    <AppProviders client={client}>
      <RouterProvider router={router} />
    </AppProviders>,
  )

  return { ...view, link, user: userEvent.setup() }
}

/* --------------------------------------------------------------- comments */

export function comment(
  seed: number,
  overrides: Partial<CommentFieldsFragment> = {},
): CommentFieldsFragment {
  return {
    __typename: 'Comment',
    id: uuid(seed),
    authorId: OTHER_USER_ID,
    body: `Comment ${String(seed)}`,
    createdAt: timestamp(seed),
    ...overrides,
  }
}

export interface PageOptions {
  hasNextPage?: boolean
  endCursor?: string | null
}

export function commentsData(
  nodes: readonly CommentFieldsFragment[],
  { hasNextPage = false, endCursor = null }: PageOptions = {},
): IssueCommentsQuery {
  return {
    issue: {
      __typename: 'Issue',
      id: ISSUE_ID,
      comments: {
        __typename: 'CommentConnection',
        nodes: [...nodes],
        pageInfo: { __typename: 'PageInfo', hasNextPage, endCursor },
      },
    },
  }
}

/**
 * The workspace's people, and who is reading.
 *
 * `me` defaults to a member, because "can I delete this" is the question this
 * document exists to answer; passing `null` is the unauthenticated case and
 * is asserted separately.
 */
export function authorsData(viewerId: string | null = VIEWER_ID): CommentAuthorsQuery {
  return {
    me: viewerId === null ? null : { __typename: 'User', id: viewerId },
    workspaceMembers: [
      {
        __typename: 'WorkspaceMember',
        userId: VIEWER_ID,
        name: 'Ada Lovelace',
        email: 'ada@example.com',
      },
      {
        __typename: 'WorkspaceMember',
        userId: OTHER_USER_ID,
        name: 'Grace Hopper',
        email: 'grace@example.com',
      },
      {
        __typename: 'WorkspaceMember',
        userId: FORMER_USER_ID,
        name: 'Alan Turing',
        email: 'alan@example.com',
      },
    ],
  }
}

export function commentCreated(created: CommentFieldsFragment): CommentCreateMutation {
  return {
    commentCreate: {
      __typename: 'CommentCreatePayload',
      comment: created,
      errors: [],
    },
  }
}

export function commentRejected(message: string): CommentCreateMutation {
  return {
    commentCreate: {
      __typename: 'CommentCreatePayload',
      comment: null,
      errors: [
        { __typename: 'ValidationErrorType', field: 'body', code: 'REQUIRED', message },
      ],
    },
  }
}

export function commentDeleted(id: string): CommentDeleteMutation {
  return {
    commentDelete: {
      __typename: 'CommentDeletePayload',
      deletedCommentId: id,
      errors: [],
    },
  }
}

/* ----------------------------------------------------------------- labels */

export function label(
  seed: number,
  overrides: Partial<LabelFieldsFragment> = {},
): LabelFieldsFragment {
  return {
    __typename: 'Label',
    id: uuid(seed),
    name: `Label ${String(seed)}`,
    color: '#0a7189',
    ...overrides,
  }
}

export function issueLabelsData(
  nodes: readonly LabelFieldsFragment[],
): IssueLabelsQuery {
  return { issue: { __typename: 'Issue', id: ISSUE_ID, labels: [...nodes] } }
}

export function workspaceLabelsData(
  nodes: readonly LabelFieldsFragment[],
  { hasNextPage = false, endCursor = null }: PageOptions = {},
): WorkspaceLabelsQuery {
  return {
    labels: {
      __typename: 'LabelConnection',
      nodes: [...nodes],
      pageInfo: { __typename: 'PageInfo', hasNextPage, endCursor },
    },
  }
}

/**
 * The attach payload: the whole issue, with the labels it now has.
 *
 * This shape is the point of the labels test -- Apollo normalises it onto the
 * `Issue:<uuid>` the panel is watching, which is why there is no
 * hand-written cache update for attach or detach.
 */
export function labelsAttached(
  nodes: readonly LabelFieldsFragment[],
): IssueLabelAttachMutation {
  return {
    issueLabelAttach: {
      __typename: 'IssueLabelPayload',
      issue: { __typename: 'Issue', id: ISSUE_ID, labels: [...nodes] },
      errors: [],
    },
  }
}

/* ------------------------------------------------- relations and children */

export function summary(
  seed: number,
  overrides: Partial<IssueSummaryFieldsFragment> = {},
): IssueSummaryFieldsFragment {
  return {
    __typename: 'IssueSummary',
    id: uuid(seed),
    title: `Issue ${String(seed)}`,
    completedAt: null,
    createdAt: timestamp(seed),
    ...overrides,
  }
}

type RelationNode = NonNullable<IssueRelationsQuery['issue']>['relations']['nodes'][number]

export function relation(
  seed: number,
  type: IssueRelationType,
  issue: IssueSummaryFieldsFragment = summary(seed + 100),
): RelationNode {
  return { __typename: 'IssueRelation', id: uuid(seed), type, issue }
}

export function relationsData(
  nodes: readonly RelationNode[],
  { hasNextPage = false, endCursor = null }: PageOptions = {},
): IssueRelationsQuery {
  return {
    issue: {
      __typename: 'Issue',
      id: ISSUE_ID,
      relations: {
        __typename: 'IssueRelationConnection',
        nodes: [...nodes],
        pageInfo: { __typename: 'PageInfo', hasNextPage, endCursor },
      },
    },
  }
}

export function relationCreated(created: RelationNode): IssueRelationCreateMutation {
  return {
    issueRelationCreate: {
      __typename: 'IssueRelationCreatePayload',
      relation: created,
      errors: [],
    },
  }
}

export function subIssuesData(
  parent: IssueSummaryFieldsFragment | null,
  children: readonly IssueSummaryFieldsFragment[],
  { hasNextPage = false, endCursor = null }: PageOptions = {},
): IssueSubIssuesQuery {
  return {
    issue: {
      __typename: 'Issue',
      id: ISSUE_ID,
      parent,
      children: {
        __typename: 'IssueSummaryConnection',
        nodes: [...children],
        pageInfo: { __typename: 'PageInfo', hasNextPage, endCursor },
      },
    },
  }
}

/**
 * The set-parent payload: the CHILD, with the parent it now has.
 *
 * The mutation always describes the child, whichever control triggered it,
 * which is the thing `useSubIssues` exists to keep components from getting
 * wrong -- so the fixture is shaped that way too rather than being made
 * convenient.
 */
export function parentSet(
  child: IssueSummaryFieldsFragment,
  parent: IssueSummaryFieldsFragment | null,
): IssueSetParentMutation {
  return {
    issueSetParent: {
      __typename: 'IssueParentPayload',
      issue: {
        __typename: 'Issue',
        id: child.id,
        title: child.title,
        completedAt: child.completedAt,
        createdAt: child.createdAt,
        parent,
      },
      errors: [],
    },
  }
}

export function searchData(
  hits: readonly { id: string; identifier: string; title: string }[],
): IssueSearchQuery {
  return {
    search: {
      __typename: 'SearchResults',
      issues: hits.map((hit) => ({ __typename: 'Issue' as const, ...hit })),
    },
  }
}
