import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { WORKSPACE_SLUG } from '../../test/factories'
import { renderApp } from '../../test/render'
import { paths } from './paths'

/**
 * Every second-wave route, and the heading its placeholder shows.
 *
 * Written as builder-plus-title rather than as a list of URL strings, which
 * is the whole point of the test: a segment spelled one way in `paths` and
 * another way in the route table produces a link that renders, matches
 * nothing, and lands on the shell's not-found page. Nothing else catches
 * that -- `paths.test.ts` proves the two *forms* of a builder agree with each
 * other, not that the router has heard of either.
 */
const SECOND_WAVE = [
  [paths.triage, 'Triage'],
  [paths.savedViews, 'Saved views'],
  [paths.favorites, 'Favorites'],
  [paths.initiatives, 'Initiatives'],
  [paths.roadmap, 'Roadmap'],
  [paths.documents, 'Documents'],
  [paths.templates, 'Templates'],
  [paths.releases, 'Releases'],
  [paths.environments, 'Environments'],
  [paths.labelGroups, 'Label groups'],
  [paths.analytics, 'Analytics'],
  [paths.semanticSearch, 'Semantic search'],
] as const satisfies readonly (readonly [(slug: string) => string, string])[]

describe('the routes whose screens are not built yet', () => {
  it.each(SECOND_WAVE)('mounts a placeholder at %o for %s', async (build, title) => {
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

    // And the page is honest about being empty rather than faking data.
    expect(screen.getByText('Not built yet')).toBeInTheDocument()

    view.unmount()
  })
})
