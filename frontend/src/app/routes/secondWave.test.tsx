import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { WORKSPACE_SLUG } from '../../test/factories'
import { renderApp } from '../../test/render'
import { paths } from './paths'

/**
 * That every `paths` builder resolves to a route the table actually mounts.
 *
 * This file began as a list of second-wave placeholders and the heading each
 * one shows. It was written as builder-plus-title rather than as URL strings,
 * which was always the point: a segment spelled one way in `paths` and another
 * way in the route table produces a link that renders, matches nothing, and
 * lands on the shell's not-found page. Nothing else catches that --
 * `paths.test.ts` proves the two *forms* of a builder agree with each other,
 * not that the router has heard of either.
 *
 * ALL TWELVE have now graduated. `analytics` was the last and the longest
 * held: `schema.graphql` had no aggregate of any kind, so the screen had
 * nothing to read and building it would have meant computing metrics on the
 * client from one page of issues and presenting them as the workspace's.
 * `workspaceAnalytics` is what changed.
 *
 * THE FILE STAYS, and this is the note that said it would. The assertion has
 * no other home: it is the only test that renders a URL a `paths` builder
 * produced and checks the router recognised it. What it asserts has inverted
 * rather than disappeared -- each row below now names a screen that must be
 * REAL, and the negative half is the load-bearing one. A route that fell
 * through to `NotFound` would still render an `<h1>`; a placeholder would
 * still render the right one. Only "the heading is right AND the page does not
 * say it is unbuilt" separates a mounted screen from either.
 *
 * `SECOND_WAVE` above it is now an empty table in `routes.tsx`, kept for the
 * same reason: a thirteenth placeholder is three strings and no wiring, and
 * its row comes back here.
 */
const GRADUATED = [
  [paths.analytics, 'Analytics'],
  [paths.roadmap, 'Roadmap'],
  [paths.releases, 'Releases'],
  [paths.semanticSearch, 'Similar issues'],
] as const satisfies readonly (readonly [(slug: string) => string, string])[]

describe('the routes whose screens were built last', () => {
  it.each(GRADUATED)('mounts a real screen at %o for %s', async (build, title) => {
    const view = renderApp({ initialPath: build(WORKSPACE_SLUG) })

    await view.link.idle()

    /*
      The heading, and not merely "something rendered". A route that matched
      nothing would still render -- the shell's `*` child catches it and shows
      `NotFound`, which is also a page with an `<h1>`. Asserting the *name*
      is what separates "this route exists" from "this route fell through".
    */
    expect(
      await screen.findByRole('heading', { level: 1, name: title }),
    ).toBeInTheDocument()

    // And it is the screen rather than the placeholder that used to stand in
    // for it. This is the half that would have failed before each graduation.
    expect(screen.queryByText('Not built yet')).not.toBeInTheDocument()

    view.unmount()
  })
})
