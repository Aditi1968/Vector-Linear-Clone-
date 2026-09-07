import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  MEMBER_ID,
  OTHER_MEMBER_ID,
  WORKSPACE_SLUG,
  membership,
  workspaceShellData,
} from '../../test/factories'
import type {
  MemberRoleUpdateMutation,
  WorkspaceInvitationListQuery,
  WorkspaceMemberListQuery,
} from '../../generated/operations'
import type { WorkspaceRole } from '../../generated/schema'

/**
 * The members screen, against the real router and a controlled network.
 *
 * What can break here is authorization theatre: a screen that hides a control
 * and then behaves as though hiding it were the enforcement. So these tests
 * check both halves -- that an ordinary member is offered nothing and asks
 * for nothing, and that a refusal arriving anyway is put on screen rather
 * than discarded.
 */

const PATH = `/${WORKSPACE_SLUG}/members`

const membersData: WorkspaceMemberListQuery = {
  workspaceMembers: [
    {
      __typename: 'WorkspaceMember',
      userId: MEMBER_ID,
      email: 'ada@example.com',
      name: 'Ada Lovelace',
      role: 'OWNER',
      createdAt: '2026-01-01T00:00:00.000Z',
    },
    {
      __typename: 'WorkspaceMember',
      userId: OTHER_MEMBER_ID,
      email: 'grace@example.com',
      name: null,
      role: 'MEMBER',
      createdAt: '2026-01-02T00:00:00.000Z',
    },
  ],
}

const invitationsData: WorkspaceInvitationListQuery = {
  invitations: [
    {
      __typename: 'WorkspaceInvitation',
      id: '00000000-0000-4000-8000-0000000000f1',
      email: 'alan@example.com',
      role: 'MEMBER',
      expiresAt: '2026-02-01T00:00:00.000Z',
      createdAt: '2026-01-03T00:00:00.000Z',
    },
  ],
}

function open(role: WorkspaceRole) {
  return renderApp({
    initialPath: PATH,
    shell: workspaceShellData([membership(WORKSPACE_SLUG, { name: 'Acme', role })]),
  })
}

describe('what an ordinary member is offered', () => {
  it('gets the people list and nothing that would be refused', async () => {
    const view = open('MEMBER')

    await view.link.resolve('WorkspaceMemberList', { data: membersData })

    expect(within(main()).getByText('Ada Lovelace')).toBeInTheDocument()
    // `name` is nullable -- an account that never set one -- and the email
    // stands in for it. Never the raw UUID.
    expect(within(main()).getByText('grace@example.com')).toBeInTheDocument()

    expect(
      within(main()).queryByRole('button', { name: /Actions for/ }),
    ).not.toBeInTheDocument()
    expect(within(main()).queryByLabelText('Email address')).not.toBeInTheDocument()
    expect(
      within(main()).getByText(
        'Only admins and owners can change roles or remove people.',
      ),
    ).toBeInTheDocument()
  })

  it('does not ask for invitations it may not read', async () => {
    const view = open('MEMBER')

    await view.link.resolve('WorkspaceMemberList', { data: membersData })

    // `invitations` returns a non-null list, so an error on it would
    // propagate to `data` and take the member list down with it if the two
    // shared an operation. Two documents, and this one is not sent.
    expect(view.link.countOf('WorkspaceInvitationList')).toBe(0)
  })
})

describe('what an admin is offered', () => {
  it('gets the role menu, the invite form and the invitation list', async () => {
    const view = open('ADMIN')

    await view.link.resolve('WorkspaceMemberList', { data: membersData })
    await view.link.resolve('WorkspaceInvitationList', { data: invitationsData })

    expect(
      within(main()).getByRole('button', { name: 'Actions for Ada Lovelace' }),
    ).toBeInTheDocument()
    expect(within(main()).getByLabelText('Email address')).toBeInTheDocument()
    expect(within(main()).getByText('alan@example.com')).toBeInTheDocument()
  })

  it('says plainly that no email is sent', async () => {
    const view = open('ADMIN')

    await view.link.resolve('WorkspaceMemberList', { data: membersData })
    await view.link.resolve('WorkspaceInvitationList', { data: invitationsData })

    // Before anyone submits, not after: discovering it afterwards is
    // discovering it too late, because the token is shown once.
    expect(within(main()).getByText('Vector cannot send email.')).toBeInTheDocument()
  })

  it('shows the invite link once, as selectable text, with a copy control', async () => {
    const view = open('ADMIN')

    await view.link.resolve('WorkspaceMemberList', { data: membersData })
    await view.link.resolve('WorkspaceInvitationList', { data: invitationsData })

    await view.user.type(within(main()).getByLabelText('Email address'), 'new@example.com')
    await view.user.click(screen.getByRole('button', { name: 'Create invite link' }))

    await view.link.resolve('MemberInvitationCreate', {
      data: {
        invitationCreate: {
          __typename: 'InvitationCreatePayload',
          invitation: {
            __typename: 'WorkspaceInvitation',
            id: '00000000-0000-4000-8000-0000000000f2',
            email: 'new@example.com',
            role: 'MEMBER',
            expiresAt: '2026-02-01T00:00:00.000Z',
            createdAt: '2026-01-04T00:00:00.000Z',
          },
          token: 'tok_abc123',
          errors: [],
        },
      },
    })

    // The token is unreadable afterwards, so the link has to be on screen as
    // text -- `navigator.clipboard` is absent on an insecure origin and can
    // be refused anywhere.
    expect(within(main()).getByText(/\/invite\/tok_abc123$/)).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Copy invite link for new@example.com' }),
    ).toBeInTheDocument()
  })

  it('renders a refusal the role said would not happen', async () => {
    const view = open('ADMIN')

    await view.link.resolve('WorkspaceMemberList', { data: membersData })

    const rejected: MemberRoleUpdateMutation = {
      memberRoleUpdate: {
        __typename: 'WorkspaceMemberPayload',
        member: null,
        errors: [
          {
            __typename: 'ValidationErrorType',
            field: 'role',
            code: 'FORBIDDEN',
            message: 'You do not have permission to change roles.',
          },
        ],
      },
    }

    await view.link.resolve('WorkspaceInvitationList', { data: invitationsData })

    await view.user.click(
      screen.getByRole('button', { name: 'Actions for Ada Lovelace' }),
    )
    await view.user.click(screen.getByRole('menuitem', { name: 'Make member' }))

    await view.link.resolve('MemberRoleUpdate', { data: rejected })

    // Hiding a button is not authorization. A viewer demoted in another tab
    // lands exactly here, and the server's answer is what the screen shows.
    expect(
      screen.getByText('You do not have permission to change roles.'),
    ).toBeInTheDocument()
  })

  it('reports a refused invitations query instead of showing an empty panel', async () => {
    const view = open('ADMIN')

    await view.link.resolve('WorkspaceMemberList', { data: membersData })
    await view.link.fail('WorkspaceInvitationList', new Error('Not found'))

    // An empty panel would read as "no invitations", which is a different and
    // wrong claim.
    expect(within(main()).getByText('Not found')).toBeInTheDocument()
    expect(within(main()).queryByText('No invitations are outstanding.')).toBeNull()
  })

  it('confirms before removing someone', async () => {
    const view = open('ADMIN')

    await view.link.resolve('WorkspaceMemberList', { data: membersData })
    await view.link.resolve('WorkspaceInvitationList', { data: invitationsData })

    await view.user.click(
      screen.getByRole('button', { name: 'Actions for Ada Lovelace' }),
    )
    await view.user.click(
      screen.getByRole('menuitem', { name: 'Remove from workspace' }),
    )

    // Nothing sent yet: removal cannot be undone from this screen.
    expect(view.link.countOf('MemberRemove')).toBe(0)
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Remove from workspace')

    await view.user.click(screen.getByRole('button', { name: 'Remove' }))

    await expect(view.link.waitForRequest('MemberRemove')).resolves.toMatchObject({
      input: { workspaceSlug: WORKSPACE_SLUG, userId: MEMBER_ID },
    })
  })
})
