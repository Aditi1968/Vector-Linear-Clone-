import type {
  IssueCreateData,
  IssueDetailData,
  IssueDetailFields,
  IssueListData,
  IssueRowFields,
  IssueValidationError,
  WorkspaceTeamsData,
} from '../features/issues/api'
import type { WorkspaceEntryQuery } from '../generated/operations'

/**
 * Server responses, built against the real operation types.
 *
 * The types come from `features/issues/api`, which is where the documents
 * live, so a field added to a document without being added here (or the
 * reverse) is a compile error rather than a test that passes against a shape
 * the server never sends. That is the whole reason these are typed rather
 * than being loose object literals: an untyped fixture is a second, private
 * definition of the schema, and it drifts.
 *
 * Nothing here invents a field. `Issue` has exactly seven, `PageInfo` has
 * exactly two, and `IssueCreatePayload` has exactly `issue` and `errors`.
 */

/**
 * A canonical hyphenated UUID from a small integer.
 *
 * Real UUIDs and not `'issue-1'`: `useIssueDetail` refuses to send an id that
 * does not match its UUID pattern and reports "not found" without a request,
 * so a fixture with a made-up id would make the detail tests assert the
 * not-found screen while believing they were testing a successful fetch.
 */
/**
 * The workspace every test in this suite operates in.
 *
 * Every field the API exposes now takes a `workspaceSlug`, and the frontend
 * reads it from `/:workspaceSlug/...` -- so a test that renders a screen
 * starts at a URL carrying this, and a fixture that answers a query is
 * answering one that named it. `src/test/render.tsx` puts it in the default
 * `initialPath`; nothing here has to repeat it.
 */
export const WORKSPACE_SLUG = 'acme'

/** The team `WorkspaceTeams` answers with, and `issueCreate` files against. */
export const TEAM_ID = '00000000-0000-4000-8000-00000000ee01'

export function issueId(seed: number): string {
  return `00000000-0000-4000-8000-${String(seed).padStart(12, '0')}`
}

/**
 * An opaque keyset cursor.
 *
 * Deliberately not a number and not parseable as one. The pagination tests
 * assert that what leaves as `after` is this token -- a value that could not
 * be an offset even by accident.
 */
export function cursor(label: string): string {
  return `Y3Vyc29yOnsiY3JlYXRlZF9hdCI6${label}`
}

/** A fixed instant, so nothing in these fixtures depends on when they run. */
const BASE_TIME = Date.parse('2026-01-15T12:00:00.000Z')

function timestamp(seed: number): string {
  return new Date(BASE_TIME - seed * 3_600_000).toISOString()
}

/** One row, with exactly the fields the list document selects. */
export function issueRow(seed: number, overrides: Partial<IssueRowFields> = {}): IssueRowFields {
  return {
    __typename: 'Issue',
    id: issueId(seed),
    title: `Issue ${seed}`,
    priority: seed % 5,
    completedAt: null,
    createdAt: timestamp(seed),
    updatedAt: timestamp(seed),
    ...overrides,
  }
}

/**
 * `count` consecutive rows starting at `from`.
 *
 * For the tests that work at real page sizes. "50 rows collapse to 25" is a
 * claim about `DEFAULT_PAGE_SIZE`-shaped pages, and shrinking it to two rows
 * to keep a fixture short would make the assertion say something weaker than
 * the behaviour it is pinning.
 */
export function issueRowRange(from: number, count: number): IssueRowFields[] {
  return Array.from({ length: count }, (_unused, index) => issueRow(from + index))
}

/** One issue with every field, as the detail query and the mutation select. */
export function issueDetail(
  seed: number,
  overrides: Partial<IssueDetailFields> = {},
): IssueDetailFields {
  return {
    ...issueRow(seed),
    description: null,
    ...overrides,
  }
}

export interface IssuePageOptions {
  hasNextPage?: boolean
  endCursor?: string | null
}

/** One page of the `issues` connection. */
export function issueListData(
  nodes: readonly IssueRowFields[],
  { hasNextPage = false, endCursor = null }: IssuePageOptions = {},
): IssueListData {
  return {
    issues: {
      __typename: 'IssueConnection',
      nodes: [...nodes],
      pageInfo: {
        __typename: 'PageInfo',
        hasNextPage,
        endCursor,
      },
    },
  }
}

/** The answer to `issue(id:)`. `null` is a successful "no such issue". */
export function issueDetailData(issue: IssueDetailFields | null): IssueDetailData {
  return { issue }
}

/** A successful `issueCreate`: an issue, and an empty error list. */
export function issueCreated(issue: IssueDetailFields): IssueCreateData {
  return {
    issueCreate: {
      __typename: 'IssueCreatePayload',
      issue,
      errors: [],
    },
  }
}

/**
 * A rejected `issueCreate`.
 *
 * This is the channel that is easy to forget exists: it arrives inside `data`
 * over an HTTP 200 with no GraphQL `errors` array, and `issue` is null exactly
 * when `errors` is non-empty.
 */
export function issueRejected(
  ...errors: readonly IssueValidationError[]
): IssueCreateData {
  return {
    issueCreate: {
      __typename: 'IssueCreatePayload',
      issue: null,
      errors: [...errors],
    },
  }
}

/**
 * One validation error.
 *
 * The `field`/`code` pairs the service actually produces are `title/REQUIRED`,
 * `title/TOO_LONG` and `priority/OUT_OF_RANGE`.
 */
export function validationError(
  field: string,
  code: string,
  message: string,
): IssueValidationError {
  return { __typename: 'ValidationErrorType', field, code, message }
}

/**
 * The answer to `WorkspaceEntry`, which is what `/` resolves through.
 *
 * An empty list is a real state -- an account in no workspace -- and is the
 * one case worth spelling out, so the argument is the slugs and not a count.
 */
export function workspaceEntryData(
  ...slugs: readonly string[]
): WorkspaceEntryQuery {
  return {
    myWorkspaces: slugs.map((slug) => ({
      __typename: 'WorkspaceMembership' as const,
      workspace: {
        __typename: 'Workspace' as const,
        slug,
        name: slug,
      },
    })),
  }
}

/**
 * The answer to `WorkspaceTeams`.
 *
 * The composer runs this: `issueCreate` requires a `teamId` and the server
 * picks no default, so a create cannot be submitted until it has answered.
 * An empty list is the state a workspace with no teams is in, which the form
 * reports rather than crashing on.
 */
export function workspaceTeamsData(
  ...teams: readonly { id: string; key: string }[]
): WorkspaceTeamsData {
  return {
    teams: teams.map((team) => ({ __typename: 'Team' as const, ...team })),
  }
}

/** The single-team workspace every create test files into. */
export function oneTeam(): WorkspaceTeamsData {
  return workspaceTeamsData({ id: TEAM_ID, key: 'ENG' })
}
