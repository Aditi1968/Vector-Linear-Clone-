import { render, screen } from '@testing-library/react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'

import { describe, expect, it } from 'vitest'

import { publicPaths } from '../../app/routes/paths'
import { LandingPage } from './LandingPage'

/**
 * The entry screen, pinned by what it must not grow back into.
 *
 * Two things break here and neither is visible in a screenshot.
 *
 *   1. Two controls answering to one name. It has happened repeatedly on this
 *      screen, because "Sign in" and "Get started" tend to appear once in a
 *      header and again in the body, and a duplicate accessible name is a
 *      screen reader user hearing the same link twice with no way to tell
 *      which is which. `getByRole` throws on more than one match, so the
 *      assertions below are the detector.
 *   2. The feature list creeping back. The approved direction is the mark,
 *      the wordmark, one line and the two ways in; everything between a
 *      visitor and the sign-in button is something they have to scroll past.
 */
function renderLanding() {
  render(
    <RouterProvider
      router={createMemoryRouter([
        { path: publicPaths.landing(), element: <LandingPage /> },
      ])}
    />,
  )
}

describe('the landing page', () => {
  it('names each of its three links exactly once', () => {
    renderLanding()

    expect(screen.getByRole('link', { name: 'Vector home' })).toHaveAttribute(
      'href',
      publicPaths.landing(),
    )
    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute(
      'href',
      publicPaths.login(),
    )
    expect(screen.getByRole('link', { name: 'Create account' })).toHaveAttribute(
      'href',
      publicPaths.register(),
    )

    // And nothing else. A fourth link is a section that grew back.
    expect(screen.getAllByRole('link')).toHaveLength(3)
  })

  it('is one heading and no feature list', () => {
    renderLanding()

    // `level: 1` throws on a second `<h1>`, and the count throws on any other
    // heading at all -- which is what a section growing back looks like.
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Vector')
    expect(screen.getAllByRole('heading')).toHaveLength(1)

    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })
})
