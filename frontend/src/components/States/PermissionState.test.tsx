import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { PermissionState } from './PermissionState'

/**
 * The access-denied screen.
 *
 * The only thing worth pinning is its default copy, and it is worth pinning
 * because it is a security property wearing the clothes of a string. Vector's
 * rule (CLAUDE.md) is that a resource which does not exist and one the caller
 * may not see must be externally indistinguishable wherever the difference
 * would leak existence. Default wording that names the thing behind the wall
 * -- "you cannot view *this project*" -- turns a list of guessed ids into an
 * oracle for which ones are real, and it is exactly the kind of edit that
 * lands in a copy review without anyone noticing what it gave away.
 */
describe('PermissionState', () => {
  it('says nothing about what is behind the wall', () => {
    render(<PermissionState />)

    const text = document.body.textContent ?? ''

    // It talks about the viewer's role, which is safe: the caller is already
    // inside something they can see.
    expect(text).toContain('role')

    // And not about the existence, identity or kind of the resource.
    for (const leak of ['project', 'issue', 'exists', 'private', 'owner']) {
      expect(text.toLowerCase()).not.toContain(leak)
    }
  })

  /*
    No `role`. This replaces a region the user navigated to -- it is the
    content of the page rather than an event that happened to it -- so it
    must not interrupt a screen reader to announce a screen already on
    display. `role="alert"` here is the same mistake `EmptyState` avoids.
  */
  it('does not announce itself as an alert', () => {
    render(<PermissionState />)

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders the way out the caller supplies', () => {
    render(<PermissionState actions={<a href="/acme/issues">Back to issues</a>} />)

    expect(screen.getByRole('link', { name: 'Back to issues' })).toBeInTheDocument()
  })
})
