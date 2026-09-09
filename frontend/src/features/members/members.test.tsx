import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { main, renderApp } from '../../test/render'
import {
  FORMER_MEMBER_ID,
  MEMBER_ID,
  OTHER_MEMBER_ID,
  WORKSPACE_SLUG,
  membership,
  workspaceShellData,
} from '../../test/factories'
import type {
  MemberRemoveMutation,
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
      removedAt: null,
    },
    {
      __typename: 'WorkspaceMember',
      userId: OTHER_MEMBER_ID,
      email: 'grace@example.com',
      name: null,
      role: 'MEMBER',
      createdAt: '2026-01-02T00:00:00.000Z',
      removedAt: null,
    },
    {
      // Somebody who has left. 026 stamps the membership rather than deleting
      // it, so the server returns this row alongside the current ones -- and
      // `role` is still ADMIN, which is exactly what makes an unmarked row
      // dangerous: it reads as a serving administrator.
      __typename: 'WorkspaceMember',
      userId: FORMER_MEMBER_ID,
      email: 'alonzo@example.com',
      name: 'Alonzo Church',
      role: 'ADMIN',
      createdAt: '2026-01-03T00:00:00.000Z',
      removedAt: '2026-06-01T00:00:00.000Z',
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

  it('marks someone who has left, and offers no way to act on them', async () => {
    const view = open('ADMIN')

    await view.link.resolve('WorkspaceMemberList', { data: membersData })
    await view.link.resolve('WorkspaceInvitationList', { data: invitationsData })

    // In words, not in colour: a dimmed row says this to some people only.
    expect(within(main()).getByText('Former member')).toBeInTheDocument()
    // The name survives, because the row exists so that what they wrote still
    // has an author.
    expect(within(main()).getByText('Alonzo Church')).toBeInTheDocument()

    // No role menu and no remove: every item in that menu is a change to a
    // membership that has already ended, and the server refuses all of them.
    // `getByRole` throwing on two matches is what keeps these names apart.
    expect(
      within(main()).queryByRole('button', { name: 'Actions for Alonzo Church' }),
    ).toBeNull()
    // Not because the menu is gone for everybody.
    expect(
      within(main()).getByRole('button', { name: 'Actions for Ada Lovelace' }),
    ).toBeInTheDocument()
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

  /** Open the dialog on Ada and press Remove. Every removal test starts here. */
  async function removeAda(view: ReturnType<typeof open>) {
    await view.link.resolve('WorkspaceMemberList', { data: membersData })
    await view.link.resolve('WorkspaceInvitationList', { data: invitationsData })

    await view.user.click(
      screen.getByRole('button', { name: 'Actions for Ada Lovelace' }),
    )
    await view.user.click(
      screen.getByRole('menuitem', { name: 'Remove from workspace' }),
    )
    await view.user.click(screen.getByRole('button', { name: 'Remove' }))
  }

  it('says which person a blocked removal is about, and why', async () => {
    const view = open('ADMIN')

    await removeAda(view)

    const refused: MemberRemoveMutation = {
      memberRemove: {
        __typename: 'MemberRemovePayload',
        removedUserId: null,
        errors: [
          {
            __typename: 'ValidationErrorType',
            field: 'userId',
            code: 'STILL_LEADS_PROJECT',
            message: 'Member still leads a project; reassign the lead first',
          },
        ],
      },
    }

    await view.link.resolve('MemberRemove', { data: refused })

    // The reason, not "The change could not be saved" -- a removal refused
    // because somebody still leads a project is a thing an admin can act on,
    // and only if the screen says so.
    expect(
      screen.getByText(/Member still leads a project; reassign the lead first/),
    ).toBeInTheDocument()
    // And who it is about. The server answers about the id it was sent, so
    // "Member" in its message names nobody a reader can see; the page is the
    // only side that knows which row this was.
    expect(screen.getByText(/Ada Lovelace was not removed/)).toBeInTheDocument()

    // Refused means unchanged: the row is still an ordinary member with its
    // menu, not one the screen has optimistically marked as gone.
    expect(within(main()).getAllByText('Former member')).toHaveLength(1)
    expect(
      within(main()).getByRole('button', { name: 'Actions for Ada Lovelace' }),
    ).toBeInTheDocument()
  })

  it('shows the removal in the list the server sends back afterwards', async () => {
    const view = open('ADMIN')

    await removeAda(view)

    await view.link.resolve('MemberRemove', {
      data: {
        memberRemove: {
          __typename: 'MemberRemovePayload',
          removedUserId: MEMBER_ID,
          errors: [],
        },
      } satisfies MemberRemoveMutation,
    })

    // The write refetches the list rather than patching the cache, so this is
    // the same answer a reload would get -- the removal is a stamped row
    // coming back from the server, not a state this screen is holding.
    await view.link.resolve('WorkspaceMemberList', {
      data: {
        workspaceMembers: membersData.workspaceMembers.map((member) =>
          member.userId === MEMBER_ID
            ? { ...member, removedAt: '2026-06-02T00:00:00.000Z' }
            : member,
        ),
      } satisfies WorkspaceMemberListQuery,
    })

    // Two people have now left, and both are marked. Ada keeps her name and
    // loses her menu.
    expect(within(main()).getAllByText('Former member')).toHaveLength(2)
    expect(within(main()).getByText('Ada Lovelace')).toBeInTheDocument()
    expect(
      within(main()).queryByRole('button', { name: 'Actions for Ada Lovelace' }),
    ).toBeNull()
    // Grace is untouched, so the marker is about `removedAt` and not about
    // the list having been refetched.
    expect(
      within(main()).getByRole('button', { name: 'Actions for grace@example.com' }),
    ).toBeInTheDocument()
  })
})
