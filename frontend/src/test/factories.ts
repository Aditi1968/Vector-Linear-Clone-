import type {
  IssueArchiveData,
  IssueCreateData,
  IssueDetailData,
  IssueDetailFields,
  IssueListData,
  IssueRowFields,
  IssueSetCycleData,
  IssueSetProjectData,
  IssueUpdateData,
  IssueValidationError,
  IssueWorkspaceContextData,
  TeamCyclesData,
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

/** The team the context answers with, and `issueCreate` files against. */
export const TEAM_ID = '00000000-0000-4000-8000-00000000ee01'

/**
 * The two workflow states that team's board has.
 *
 * Two and not one, because every interesting assertion about editing a status
 * is about moving between them -- and because `completedAt` is derived from
 * the category, so a suite with only an UNSTARTED state can never exercise
 * the completed branch.
 */
export const TODO_STATE_ID = '00000000-0000-4000-8000-00000000ff01'
export const DONE_STATE_ID = '00000000-0000-4000-8000-00000000ff02'

/** The two people in the workspace, for assignee round-trips. */
export const MEMBER_ID = '00000000-0000-4000-8000-00000000aa01'
export const OTHER_MEMBER_ID = '00000000-0000-4000-8000-00000000aa02'

export const PROJECT_ID = '00000000-0000-4000-8000-00000000bb01'
export const CYCLE_ID = '00000000-0000-4000-8000-00000000cc01'

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
    teamId: TEAM_ID,
    // The name the issue is known by outside the product. Seeded from the
    // same number as the id so a test can name a row without inventing one.
    identifier: `ENG-${String(seed)}`,
    title: `Issue ${seed}`,
    priority: seed % 5,
    workflowStateId: TODO_STATE_ID,
    assigneeId: null,
    estimate: null,
    dueDate: null,
    completedAt: null,
    createdAt: timestamp(seed),
    updatedAt: timestamp(seed),
    labels: [],
    project: null,
    cycle: null,
    ...overrides,
  }
}

/** One label, as a row and the detail view select them. */
export function label(name: string, color = '#0a7189') {
  return { __typename: 'Label' as const, id: `label-${name}`, name, color }
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
    creatorId: null,
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
 * The answer to `IssueWorkspaceContext`.
 *
 * One fixture for the whole lookup layer, because it is one document: the
 * workflow states rows draw their status glyph from, the people an assignee
 * id resolves to, the projects an issue can be moved into, and the team
 * `issueCreate` files against.
 *
 * Deliberately not parameterised beyond overrides. Almost every test wants
 * "an ordinary workspace", and the two that want something else (no teams,
 * an unknown state) say so by overriding one key rather than by assembling
 * the whole thing.
 */
export function workspaceContextData(
  overrides: Partial<IssueWorkspaceContextData> = {},
): IssueWorkspaceContextData {
  return {
    teams: [
      {
        __typename: 'Team',
        id: TEAM_ID,
        key: 'ENG',
        name: 'Engineering',
        workflowStates: [
          {
            __typename: 'WorkflowState',
            id: TODO_STATE_ID,
            name: 'Todo',
            category: 'UNSTARTED',
            position: 0,
            color: null,
          },
          {
            __typename: 'WorkflowState',
            id: DONE_STATE_ID,
            name: 'Done',
            category: 'COMPLETED',
            position: 1,
            color: null,
          },
        ],
      },
    ],
    workspaceMembers: [
      {
        __typename: 'WorkspaceMember',
        userId: MEMBER_ID,
        name: 'Ada Lovelace',
        email: 'ada@example.com',
      },
      {
        // No name, which is a real state -- an invited account that has never
        // set one -- and the case where the email has to stand in for it.
        __typename: 'WorkspaceMember',
        userId: OTHER_MEMBER_ID,
        name: null,
        email: 'grace@example.com',
      },
    ],
    projects: {
      __typename: 'ProjectConnection',
      nodes: [
        {
          __typename: 'Project',
          id: PROJECT_ID,
          name: 'Platform',
          state: 'STARTED',
        },
      ],
    },
    ...overrides,
  }
}

/** The answer to `TeamCycles`, which only the open inspector asks for. */
export function teamCyclesData(): TeamCyclesData {
  return {
    cycles: [
      {
        __typename: 'Cycle',
        id: CYCLE_ID,
        number: 12,
        name: null,
        startsAt: timestamp(48),
        endsAt: timestamp(0),
      },
    ],
  }
}

/** A successful `issueUpdate`. */
export function issueUpdated(issue: IssueDetailFields): IssueUpdateData {
  return {
    issueUpdate: { __typename: 'IssueUpdatePayload', issue, errors: [] },
  }
}

/**
 * A rejected `issueUpdate`.
 *
 * The channel that is easy to forget exists: it arrives inside `data` over an
 * HTTP 200 with no GraphQL `errors` array, and `issue` is null exactly when
 * `errors` is non-empty.
 */
export function issueUpdateRejected(
  ...errors: readonly IssueValidationError[]
): IssueUpdateData {
  return {
    issueUpdate: {
      __typename: 'IssueUpdatePayload',
      issue: null,
      errors: [...errors],
    },
  }
}

export function issueProjectSet(issue: IssueDetailFields): IssueSetProjectData {
  return {
    issueSetProject: {
      __typename: 'IssueSetProjectPayload',
      issue,
      errors: [],
    },
  }
}

export function issueCycleSet(issue: IssueDetailFields): IssueSetCycleData {
  return {
    issueSetCycle: { __typename: 'IssueSetCyclePayload', issue, errors: [] },
  }
}

/** A successful `issueArchive`, which returns only the id. */
export function issueArchived(id: string): IssueArchiveData {
  return {
    issueArchive: {
      __typename: 'IssueArchivePayload',
      issue: { __typename: 'Issue', id },
      errors: [],
    },
  }
}
