import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import {
  expectEveryButtonNamed,
  expectHeadingLevelsUnbroken,
  expectNoDuplicateButtonNames,
  expectOneFirstLevelHeading,
} from '../../test/a11y'
import { main, renderApp } from '../../test/render'
import {
  MEMBER_ID,
  TEAM_ID,
  TODO_STATE_ID,
  WORKSPACE_SLUG,
  workspaceContextData,
} from '../../test/factories'
import type { EmbeddingIndexStateQuery } from '../../generated/operations'
import { describeCoverage, explainEmptyAnswer, similarityPercent } from './lib/indexState'

/**
 * The semantic-search screen, against the real router, cache and a controlled
 * network.
 *
 * ## The claim this whole screen exists to make
 *
 * An empty answer from `issueDuplicateSuggestions` means one of two completely
 * different things -- nothing is similar, or nothing is indexed -- and the
 * server returns the same empty list for both. `SearchService.indexing_state`
 * names the defect outright: "every interface built on it has been rendering
 * 'no possible duplicates' over a workspace that has never been embedded".
 *
 * So: **this screen may say "nothing is near enough in meaning" only when the
 * index is enabled, populated, and has nothing waiting.** The tests below are
 * that rule, from both directions.
 */

const SEMANTIC_PATH = `/${WORKSPACE_SLUG}/semantic-search`

const ISSUE_ID = '00000000-0000-4000-8000-0000000ba001'

function indexState(
  overrides: Partial<EmbeddingIndexStateQuery['embeddingIndexingState']> = {},
): EmbeddingIndexStateQuery['embeddingIndexingState'] {
  return {
    __typename: 'EmbeddingIndexingState',
    indexed: 120,
    pending: 0,
    failed: 0,
    enabled: true,
    ...overrides,
  }
}

function match(similarity: number) {
  return {
    __typename: 'DuplicateSuggestion' as const,
    similarity,
    issue: {
      __typename: 'Issue' as const,
      id: ISSUE_ID,
      identifier: 'ENG-42',
      title: 'CI fails intermittently',
      priority: 2,
      teamId: TEAM_ID,
      workflowStateId: TODO_STATE_ID,
      assigneeId: MEMBER_ID,
    },
  }
}

async function openSemanticSearch(
  state: EmbeddingIndexStateQuery['embeddingIndexingState'] = indexState(),
) {
  const app = renderApp({ initialPath: SEMANTIC_PATH })

  await app.link.resolve('IssueWorkspaceContext', { data: workspaceContextData() })
  await app.link.resolve('EmbeddingIndexState', {
    data: { embeddingIndexingState: state },
  })

  return app
}

async function search(
  app: Awaited<ReturnType<typeof openSemanticSearch>>,
  matches: readonly ReturnType<typeof match>[],
) {
  await app.user.type(
    screen.getByLabelText('Describe the issue'),
    'the build keeps dying',
  )
  await app.user.click(screen.getByRole('button', { name: 'Find similar issues' }))

  await app.link.resolve('SemanticIssueMatches', {
    data: { issueDuplicateSuggestions: matches },
  })
}

describe('the semantic search screen', () => {
  it('sends the text untouched, with a null description when none was typed', async () => {
    const app = await openSemanticSearch()

    await app.user.type(
      screen.getByLabelText('Describe the issue'),
      'the build keeps dying',
    )
    await app.user.click(screen.getByRole('button', { name: 'Find similar issues' }))

    const variables = await app.link.waitForRequest('SemanticIssueMatches')

    expect(variables).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
      title: 'the build keeps dying',
      // Null and not `''`: the schema declares this nullable, and an empty
      // string is a description that happens to be empty -- which would be
      // embedded.
      description: null,
    })

    app.unmount()
  })

  it('sends nothing at all until something is typed', async () => {
    const app = await openSemanticSearch()

    expect(app.link.countOf('SemanticIssueMatches')).toBe(0)
    expect(screen.getByText('Describe an issue and search.')).toBeInTheDocument()

    app.unmount()
  })

  it('reads the index before anybody types', async () => {
    // The point of ordering it this way: the screen has to be able to say what
    // it is capable of finding BEFORE it reports finding nothing.
    const app = renderApp({ initialPath: SEMANTIC_PATH })

    expect(await app.link.waitForRequest('EmbeddingIndexState')).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
    })

    app.unmount()
  })

  it('shows a match with its similarity, named as a similarity', async () => {
    const app = await openSemanticSearch()

    await search(app, [match(0.8731)])

    const list = within(main()).getByRole('list', { name: 'Issues near in meaning' })

    expect(
      within(list).getByRole('link', { name: /ENG-42 CI fails intermittently/ }),
    ).toHaveAttribute('href', `/${WORKSPACE_SLUG}/issues/${ISSUE_ID}`)

    // "similar" and not "match": this is a cosine distance, not a probability
    // that the two are the same issue.
    expect(within(list).getByText('87% similar')).toBeInTheDocument()

    app.unmount()
  })

  it('does NOT say nothing matches when the deployment has no index', async () => {
    const app = await openSemanticSearch(
      // `enabled: false` returns all three counts as zero WITHOUT LOOKING, so
      // zero here is not a count of anything.
      indexState({ enabled: false, indexed: 0, pending: 0, failed: 0 }),
    )

    await search(app, [])

    expect(screen.getByText('Semantic search is not available here')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing is near enough/)).not.toBeInTheDocument()

    // And no counts are drawn, because three zeroes beside each other would be
    // indistinguishable from a workspace with no issues.
    expect(screen.queryByText('Indexed')).not.toBeInTheDocument()

    app.unmount()
  })

  it('does NOT say nothing matches while issues are still waiting to be indexed', async () => {
    const app = await openSemanticSearch(indexState({ indexed: 40, pending: 80 }))

    await search(app, [])

    expect(
      screen.getByText('No matches among the issues indexed so far'),
    ).toBeInTheDocument()
    expect(screen.getByText(/80 issues are still waiting/)).toBeInTheDocument()

    app.unmount()
  })

  it('says nothing matches only when the index is complete', async () => {
    const app = await openSemanticSearch(indexState({ indexed: 120, pending: 0 }))

    await search(app, [])

    expect(screen.getByText('Nothing is near enough in meaning')).toBeInTheDocument()

    app.unmount()
  })

  it('warns that a full-looking answer is partial too', async () => {
    // The caveat is not only on the empty result. Ten matches out of a
    // half-built index are not the ten best matches.
    const app = await openSemanticSearch(indexState({ indexed: 40, pending: 80 }))

    await search(app, [match(0.9)])

    expect(
      screen.getByText(/Results are incomplete while anything is waiting/),
    ).toBeInTheDocument()

    app.unmount()
  })

  it('indexes a batch on demand and reports what the sweep wrote', async () => {
    // A deployment may run no background worker, and then this mutation is the
    // only thing in the system that ever writes an embedding.
    const app = await openSemanticSearch(indexState({ indexed: 0, pending: 100 }))

    await app.user.click(screen.getByRole('button', { name: 'Index a batch now' }))

    expect(await app.link.waitForRequest('EmbeddingsRefresh')).toEqual({
      workspaceSlug: WORKSPACE_SLUG,
    })

    await app.link.resolve('EmbeddingsRefresh', { data: { embeddingsRefresh: 100 } })

    expect(await screen.findByText('That sweep indexed 100 issues.')).toBeInTheDocument()

    app.unmount()
  })

  it('is navigable', async () => {
    const app = await openSemanticSearch()

    await search(app, [match(0.9)])

    expectOneFirstLevelHeading('Semantic search')
    expectHeadingLevelsUnbroken()
    expectEveryButtonNamed()
    expectNoDuplicateButtonNames()

    app.unmount()
  })
})

describe('what an empty answer is allowed to say', () => {
  it('refuses to claim completeness without an index state', () => {
    // Null is "we have not been told", which is not the same as "nothing
    // matches" and must not be reported as it.
    expect(explainEmptyAnswer(null).isComplete).toBe(false)
  })

  it.each([
    ['disabled', indexState({ enabled: false, indexed: 0 })],
    ['empty', indexState({ indexed: 0 })],
    ['still building', indexState({ indexed: 40, pending: 80 })],
  ])('refuses to claim completeness when the index is %s', (_case, state) => {
    const answer = explainEmptyAnswer(state)

    expect(answer.isComplete).toBe(false)
    expect(answer.title).not.toMatch(/near enough/)
  })

  it('claims completeness only when enabled, populated and idle', () => {
    expect(explainEmptyAnswer(indexState()).isComplete).toBe(true)
  })

  it('reports a poison row without suppressing the claim', () => {
    // A failed issue is a PERMANENT gap. Waiting for it to clear would mean
    // this screen could never say "nothing matches" again -- so it is reported
    // beside the answer instead of blocking it.
    expect(explainEmptyAnswer(indexState({ failed: 3 })).isComplete).toBe(true)
    expect(describeCoverage(indexState({ failed: 3 }))).toMatch(/3 failed/)
  })
})

describe('similarity', () => {
  it('rounds to a whole percent and clamps out-of-range values', () => {
    expect(similarityPercent(0.8731)).toBe(87)
    expect(similarityPercent(0)).toBe(0)
    // A percentage over 100 would read as a bug in the display rather than in
    // whatever produced it.
    expect(similarityPercent(1.4)).toBe(100)
    expect(similarityPercent(-0.2)).toBe(0)
  })
})
