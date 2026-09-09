import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { renderApp } from '../../test/render'
import { WORKSPACE_SLUG, workspaceContextData } from '../../test/factories'

/**
 * "There are none" and "we could not find out" are different sentences.
 *
 * `useWorkspaceContext` answers most screens' *lookups* -- which person is
 * `assigneeId`, which state is `workflowStateId` -- and for those a failure
 * that leaves the maps empty is the right degradation: a row shows ids instead
 * of names rather than the whole list being replaced by an error about a name
 * resolution. That part is deliberate and unchanged.
 *
 * Two screens use it differently. The board and the triage queue cannot be
 * drawn at all without a team, and both said so in as many words: "No teams in
 * this workspace". An empty array is also what a dropped connection leaves
 * behind, so a network failure made both of them assert a fact about the
 * workspace that nothing had measured -- with no error, no retry, and no way
 * for the reader to tell the two situations apart. Somebody would go and
 * create a team they already had.
 *
 * The board had a second version of the same mistake on the loading path:
 * `/board?team=ENG` announced "No team with the key ENG" underneath its own
 * spinner, because `teams` is empty before the answer arrives just as it is
 * after a failure.
 *
 * These live in `features/issues` because that is where the hook lives and
 * where the fix is; the two screens are the callers that made the claim.
 */

const CONTEXT = 'IssueWorkspaceContext'

describe.each([
  { screen: 'the board', path: `/${WORKSPACE_SLUG}/board` },
  { screen: 'the triage queue', path: `/${WORKSPACE_SLUG}/triage` },
])('$screen when the team lookup fails', ({ path }) => {
  it('says the lookup failed instead of that the workspace has no teams', async () => {
    const app = renderApp({ initialPath: path })

    await app.link.fail(CONTEXT, new Error('Network unreachable'))

    expect(screen.queryByText('No teams in this workspace')).not.toBeInTheDocument()

    const failure = await screen.findByRole('alert')

    expect(failure).toHaveTextContent('Could not load teams')
    expect(failure).toHaveTextContent('Network unreachable')
  })

  it('offers a way back, and takes it', async () => {
    // An error state with no recovery path is a dead end, and the whole
    // screen is behind this one query.
    const app = renderApp({ initialPath: path })

    await app.link.fail(CONTEXT, new Error('Network unreachable'))

    await app.user.click(screen.getByRole('button', { name: 'Try again' }))
    await app.link.resolve(CONTEXT, { data: workspaceContextData() })

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('No teams in this workspace')).not.toBeInTheDocument()
  })

  it('still says there are none when the server answers with none', async () => {
    // The other half. A screen that reported every empty workspace as a
    // failure would be the same bug facing the other way.
    const app = renderApp({ initialPath: path })

    await app.link.resolve(CONTEXT, {
      data: workspaceContextData({ teams: [], workspaceMembers: [] }),
    })

    expect(await screen.findByText('No teams in this workspace')).toBeInTheDocument()
    expect(screen.queryByText('Could not load teams')).not.toBeInTheDocument()
  })
})

describe('the board addressed with a team key', () => {
  it('does not call the key unknown while the teams are still loading', async () => {
    const app = renderApp({ initialPath: `/${WORKSPACE_SLUG}/board?team=ENG` })

    // Nothing answered yet: `teams` is empty, and `team` is undefined because
    // of that rather than because the key names nothing.
    await app.link.idle()

    expect(screen.queryByText(/No team with the key/)).not.toBeInTheDocument()
  })

  it('does call it unknown once the teams have arrived without it', async () => {
    const app = renderApp({ initialPath: `/${WORKSPACE_SLUG}/board?team=NOPE` })

    await app.link.resolve(CONTEXT, { data: workspaceContextData() })

    expect(await screen.findByText('No team with the key NOPE')).toBeInTheDocument()
  })
})
