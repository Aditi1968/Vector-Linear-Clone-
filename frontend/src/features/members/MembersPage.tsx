import { useCallback, useId, useState } from 'react'

import { PageContent, PageHeader } from '../../app/layout'
import { useWorkspace } from '../../app/workspace'
import {
  Avatar,
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  Input,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Menu,
  Select,
  Spinner,
  TeamIcon,
  VisuallyHidden,
} from '../../components'
import type { MenuItem } from '../../components'
import { formatAbsolute } from '../issues/lib/dates'
import { onboardingPaths } from '../onboarding/lib/progress'
import shared from '../screens.module.css'
import styles from './Members.module.css'
import {
  useInvitations,
  useMemberActions,
  useMembers,
} from './api'
import type {
  CreatedInvitation,
  MemberValidationError,
  WorkspaceMemberRow,
  WorkspaceRole,
} from './api'

const NO_ERRORS: readonly MemberValidationError[] = []

/**
 * The roles `invitationCreate` and `memberRoleUpdate` accept.
 *
 * `WorkspaceRole` from the generated schema is the source of the values; the
 * sentences are this application's explanation of them. Typed against the
 * schema union so that a role added or removed there fails to compile here.
 */
const ROLES: readonly { value: WorkspaceRole; label: string; short: string }[] = [
  { value: 'MEMBER', label: 'Member -- can work on issues', short: 'Member' },
  { value: 'ADMIN', label: 'Admin -- can also manage teams and people', short: 'Admin' },
  { value: 'OWNER', label: 'Owner -- full control of the workspace', short: 'Owner' },
]

function roleLabel(role: WorkspaceRole): string {
  return ROLES.find((entry) => entry.value === role)?.short ?? role
}

/** What to call someone. `name` is nullable -- an account that never set one. */
function memberName(member: WorkspaceMemberRow): string {
  return member.name ?? member.email
}

/** One created invitation, with the link built from its token. */
interface IssuedInvite extends CreatedInvitation {
  link: string
}

/**
 * Everyone in the workspace, the role each holds, and outstanding invitations.
 *
 * ## Hiding a button is not authorization
 *
 * `useWorkspace().role` decides what this screen *offers*. It decides nothing
 * about what is allowed: the server authorizes `memberRoleUpdate`,
 * `memberRemove`, `invitationCreate` and `invitationRevoke` against the
 * caller's own membership, and refuses regardless of what the client sent or
 * believed. So the role here is used twice and for two different jobs --
 * to avoid offering a control that would only fail, and to avoid asking for
 * `invitations`, which admins-and-owners-only field would otherwise return an
 * error this screen would discard -- and every refusal that arrives anyway is
 * rendered. A viewer promoted or demoted in another tab is exactly the case
 * that produces one.
 *
 * ## There is no email
 *
 * `invitationCreate` mints a token, returns it **once**, and no query can
 * read it back. If this screen does not put it in front of the person who
 * asked for it, the invitation is gone. So each created invitation stays on
 * screen with its link and a copy control, the link is selectable text as
 * well -- `navigator.clipboard` is absent on an insecure origin and can be
 * refused anywhere -- and the panel says in plain words that nothing was
 * sent. `features/onboarding` makes the same admission during setup, and
 * the accept URL is built from its path helper rather than written out.
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`; this renders a fragment and adds neither.
 */
export function MembersPage() {
  const { role, viewer, workspace } = useWorkspace()
  const canManage = role === 'ADMIN' || role === 'OWNER'

  const { members, isLoading, errorMessage, retry } = useMembers()
  const invitationsQuery = useInvitations(canManage)
  const { invite, revokeInvitation, changeRole, removeMember, isSubmitting } =
    useMemberActions()

  const [email, setEmail] = useState('')
  const [inviteRole, setInviteRole] = useState<WorkspaceRole>('MEMBER')
  const [issued, setIssued] = useState<IssuedInvite[]>([])
  const [formErrors, setFormErrors] = useState<readonly MemberValidationError[]>(NO_ERRORS)
  const [actionError, setActionError] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(null)
  const [pendingRemoval, setPendingRemoval] = useState<WorkspaceMemberRow | null>(null)

  const baseId = useId()
  const emailId = `${baseId}-email`
  const emailErrorId = `${baseId}-email-error`
  const roleId = `${baseId}-role`

  const emailErrors = formErrors.filter((entry) => entry.field === 'email')
  const otherErrors = formErrors.filter((entry) => entry.field !== 'email')

  /**
   * Report one outcome the same way wherever it came from.
   *
   * `toForm` is what keeps a rejected role change out of the invite form's
   * `email` field. Only the invite has fields for a rejection to name; every
   * other write on this screen owns no control, so its refusal belongs at the
   * top of the page and nowhere else.
   *
   * `prefix` names who a refusal is about. A removal is refused by the server
   * with a reason and no subject -- "Member still leads a project; reassign
   * the lead first" -- because the server is answering about the id it was
   * sent. Read at the top of a list of people, that sentence is about nobody
   * in particular, and the person it refers to is the one thing the screen
   * already knows.
   */
  const report = useCallback(
    (
      outcome:
        | { status: 'ok' }
        | { status: 'rejected'; errors: readonly MemberValidationError[] }
        | { status: 'failed'; message: string },
      success: string,
      toForm = false,
      prefix = '',
    ): boolean => {
      setFormErrors(NO_ERRORS)

      if (outcome.status === 'ok') {
        setActionError(null)
        setStatus(success)
        return true
      }

      setStatus(null)

      if (outcome.status === 'rejected') {
        if (toForm) {
          setFormErrors(outcome.errors)
          setActionError(null)
        } else {
          setActionError(
            prefix + outcome.errors.map((entry) => entry.message).join(' '),
          )
        }

        return false
      }

      setActionError(prefix + outcome.message)
      return false
    },
    [],
  )

  const handleInvite = useCallback(() => {
    void invite(email, inviteRole).then((outcome) => {
      if (!report(outcome, `Invitation created for ${email}.`, true)) {
        return
      }

      if (outcome.status !== 'ok') {
        return
      }

      setIssued((current) => [
        ...current,
        {
          ...outcome.value,
          // Absolute, because this goes into a chat message somebody else
          // opens. `window.location.origin` and not a configured base URL:
          // the link has to work from wherever this app is actually served,
          // and that is the only thing that knows.
          link: `${window.location.origin}${onboardingPaths.acceptInvite(outcome.value.token)}`,
        },
      ])
      setEmail('')
    })
  }, [email, invite, inviteRole, report])

  const copy = useCallback((invitation: IssuedInvite) => {
    // `navigator.clipboard` is not always there -- an insecure origin, an old
    // browser, a refused permission -- and the failure is silent. Unreported,
    // it leaves someone believing they hold a link that is not on their
    // clipboard, and this is the one string in the product that cannot be
    // fetched again.
    void navigator.clipboard
      ?.writeText(invitation.link)
      .then(() => {
        setStatus(`Invite link for ${invitation.invitation.email} copied.`)
      })
      .catch(() => {
        setStatus('Could not copy. Select the link and copy it manually.')
      })
  }, [])

  return (
    <>
      <PageHeader
        title="Members"
        description={`Everyone in ${workspace.name}, and the role each holds.`}
      />

      <PageContent>
        <div className={shared.stack}>
          {actionError !== null && (
            <p className={shared.formError} role="alert">
              {actionError}
            </p>
          )}
          <div role="status">
            <VisuallyHidden as="div">{status ?? ''}</VisuallyHidden>
          </div>

          <section className={shared.panel} aria-labelledby="members-people">
            <div className={shared.panelHeader}>
              <h2 className={styles.sectionLabel} id="members-people">
                People
              </h2>
              {!canManage && (
                <span className={shared.footnote}>
                  Only admins and owners can change roles or remove people.
                </span>
              )}
            </div>

            <div className={shared.panelBody}>
              {isLoading && <Spinner label="Loading members" />}

              {!isLoading && errorMessage !== null && members.length === 0 && (
                <ErrorState
                  title="Could not load members"
                  description={errorMessage}
                  onRetry={retry}
                />
              )}

              {!isLoading && errorMessage === null && members.length === 0 && (
                /* Close to unreachable -- the viewer is a member, so the list
                   contains at least them -- but a blank panel is not an
                   answer, and this is a fact about the data rather than a
                   failure of the screen. */
                <EmptyState
                  icon={<TeamIcon />}
                  title="No members listed"
                  description="This workspace reports nobody in it, which should not be possible while you are looking at it."
                />
              )}

              {members.length > 0 && (
                <List label="Workspace members">
                  {members.map((member) => {
                    const isSelf = member.userId === viewer?.id
                    // Since 026 a removal stamps the membership instead of
                    // deleting it, so this list is the roster *and* the
                    // record of who used to be on it. Without this the two
                    // render identically -- role menu included -- and the
                    // screen offers to promote somebody who left.
                    const isFormer = member.removedAt !== null

                    const items: readonly MenuItem[] = [
                      ...ROLES.map((entry) => ({
                        id: entry.value,
                        label:
                          member.role === entry.value
                            ? `${entry.short} (current)`
                            : `Make ${entry.short.toLowerCase()}`,
                        disabled: isSubmitting || member.role === entry.value,
                        onSelect: () => {
                          void changeRole(member.userId, entry.value).then((outcome) => {
                            report(
                              outcome,
                              `${memberName(member)} is now ${entry.short.toLowerCase()}.`,
                            )
                          })
                        },
                      })),
                      {
                        id: 'remove',
                        label: 'Remove from workspace',
                        destructive: true,
                        disabled: isSubmitting,
                        separatorBefore: true,
                        onSelect: () => {
                          // Confirmed, never immediate: removal cannot be
                          // undone from this screen, and the person has to be
                          // re-invited.
                          setPendingRemoval(member)
                        },
                      },
                    ]

                    return (
                      <ListRow key={member.userId}>
                        <ListRowMain>
                          <Avatar name={memberName(member)} size="sm" decorative />{' '}
                          {memberName(member)}
                          {isSelf && <VisuallyHidden>, you</VisuallyHidden>}
                          {/* Only when it adds something. `memberName` falls
                              back to the email for an account that never set
                              a name, and printing it twice reads as a bug. */}
                          {member.name !== null && (
                            <span className={shared.rowSub}>{member.email}</span>
                          )}
                        </ListRowMain>

                        <ListRowMeta>
                          {/* The word, not a colour and not a dimmed row.
                              Read before the role, because "Owner" on its own
                              is the thing this marker has to contradict. */}
                          {isFormer && (
                            <span className={styles.former}>Former member</span>
                          )}

                          {/* Readouts, not pills. `roleLabel` still returns
                              "Admin" rather than "ADMIN" -- the capitals are
                              CSS, so what a screen reader receives is a word
                              and not an acronym it may spell out.

                              Still shown for someone who left: it is what they
                              held, which is what a row about the past is for,
                              and it grants nothing -- `find_membership`
                              filters `removed_at IS NULL`, so their session
                              carries no permission at all. */}
                          <span className={styles.role}>
                            {roleLabel(member.role)}
                          </span>

                          {member.removedAt === null ? (
                            <time
                              className={styles.joined}
                              dateTime={member.createdAt}
                              title={member.createdAt}
                            >
                              Joined {formatAbsolute(member.createdAt)}
                            </time>
                          ) : (
                            /* When they left, in place of when they joined.
                               One date per row: the joining date of somebody
                               who is gone is the less useful of the two. */
                            <time
                              className={styles.joined}
                              dateTime={member.removedAt}
                              title={member.removedAt}
                            >
                              Removed {formatAbsolute(member.removedAt)}
                            </time>
                          )}

                          {/* No menu for a former member: every item in it is
                              a change to a membership that has already ended,
                              and the server refuses all of them -- `update_role`
                              and `mark_removed` both filter on
                              `removed_at IS NULL`. Offering them would be a
                              screen promising what the backend will not do. */}
                          {canManage && !isFormer && (
                            <Menu
                              label={`Actions for ${memberName(member)}`}
                              items={items}
                              size="sm"
                              align="end"
                            />
                          )}
                        </ListRowMeta>
                      </ListRow>
                    )
                  })}
                </List>
              )}
            </div>
          </section>

          {canManage && (
            <section className={shared.panel} aria-labelledby="members-invites">
              <div className={shared.panelHeader}>
                <h2 className={styles.sectionLabel} id="members-invites">
                  Invitations
                </h2>
              </div>

              <div className={shared.panelBody}>
                {/* Stated before anyone submits, not after. Discovering that
                    no email was sent *after* closing the page is discovering
                    it too late. */}
                <p className={shared.notice}>
                  <strong>Vector cannot send email.</strong> Creating an
                  invitation gives you a link, shown here once and never again.
                  You send it to the person yourself.
                </p>

                <form
                  className={shared.form}
                  noValidate
                  onSubmit={(event) => {
                    event.preventDefault()
                    handleInvite()
                  }}
                >
                  {otherErrors.length > 0 && (
                    <p className={shared.fieldError} role="alert">
                      {otherErrors.map((entry) => entry.message).join(' ')}
                    </p>
                  )}

                  <div className={shared.fieldRow}>
                    <div className={shared.field}>
                      <label className={shared.label} htmlFor={emailId}>
                        Email address
                      </label>
                      <Input
                        id={emailId}
                        // `type="email"` for the keyboard it brings up on a
                        // phone. The browser's own validity check is off
                        // (`noValidate`), so the server's rule decides.
                        type="email"
                        autoComplete="off"
                        value={email}
                        invalid={emailErrors.length > 0}
                        aria-describedby={
                          emailErrors.length > 0 ? emailErrorId : undefined
                        }
                        onChange={(event) => {
                          setEmail(event.target.value)
                        }}
                      />
                      {emailErrors.length > 0 && (
                        <p className={shared.fieldError} id={emailErrorId}>
                          {emailErrors.map((entry) => entry.message).join(' ')}
                        </p>
                      )}
                    </div>

                    <div className={shared.field}>
                      <label className={shared.label} htmlFor={roleId}>
                        Role
                      </label>
                      <Select
                        id={roleId}
                        value={inviteRole}
                        onChange={(event) => {
                          // Narrowed against the same list the options come
                          // from, so a value that is not a role cannot reach
                          // the mutation.
                          const chosen = ROLES.find(
                            (entry) => entry.value === event.target.value,
                          )

                          if (chosen !== undefined) {
                            setInviteRole(chosen.value)
                          }
                        }}
                      >
                        {ROLES.map((entry) => (
                          <option key={entry.value} value={entry.value}>
                            {entry.label}
                          </option>
                        ))}
                      </Select>
                    </div>

                    <Button type="submit" variant="primary" loading={isSubmitting}>
                      Create invite link
                    </Button>
                  </div>
                </form>

                {issued.length > 0 && (
                  <>
                    <h3 className={styles.sectionLabel}>Links to send</h3>
                    <ul className={shared.stack}>
                      {issued.map((invitation) => (
                        <li className={styles.issuedInvite} key={invitation.invitation.id}>
                          <p className={shared.footnote}>
                            {invitation.invitation.email} &middot;{' '}
                            {roleLabel(invitation.invitation.role)}
                          </p>
                          {/* The link as selectable text and not only behind
                              a button, because the clipboard can be refused
                              and this string cannot be fetched again. */}
                          <p className={shared.secret}>{invitation.link}</p>
                          {/*
                            `aria-label` rather than appended hidden text.
                            An accessible name concatenates each node's
                            *trimmed* text, so " for ada@..." would join as
                            "Copy linkfor ada@...". Stating the whole name is
                            simpler than smuggling a separator into it.
                          */}
                          <Button
                            size="sm"
                            aria-label={`Copy invite link for ${invitation.invitation.email}`}
                            onClick={() => {
                              copy(invitation)
                            }}
                          >
                            Copy link
                          </Button>
                        </li>
                      ))}
                    </ul>
                  </>
                )}

                {invitationsQuery.isLoading && <Spinner label="Loading invitations" />}

                {invitationsQuery.errorMessage !== null && (
                  /* The server refused, though the role said it would not.
                     Rendered rather than swallowed: a demotion in another tab
                     lands exactly here, and an empty panel would read as "no
                     invitations". */
                  <p className={shared.formError} role="alert">
                    {invitationsQuery.errorMessage}
                  </p>
                )}

                {!invitationsQuery.isLoading &&
                  invitationsQuery.errorMessage === null &&
                  invitationsQuery.invitations.length === 0 && (
                    <p className={shared.footnote}>
                      No invitations are outstanding.
                    </p>
                  )}

                {invitationsQuery.invitations.length > 0 && (
                  <List label="Outstanding invitations">
                    {invitationsQuery.invitations.map((invitation) => (
                      <ListRow key={invitation.id}>
                        <ListRowMain>
                          {invitation.email}
                          <span className={shared.rowSub}>
                            Expires {formatAbsolute(invitation.expiresAt)}
                          </span>
                        </ListRowMain>

                        <ListRowMeta>
                          <span className={styles.role}>
                            {roleLabel(invitation.role)}
                          </span>

                          <Button
                            size="sm"
                            variant="danger"
                            disabled={isSubmitting}
                            aria-label={`Revoke the invitation for ${invitation.email}`}
                            onClick={() => {
                              void revokeInvitation(invitation.id).then((outcome) => {
                                report(
                                  outcome,
                                  `Invitation for ${invitation.email} revoked.`,
                                )
                              })
                            }}
                          >
                            Revoke
                          </Button>
                        </ListRowMeta>
                      </ListRow>
                    ))}
                  </List>
                )}
              </div>
            </section>
          )}
        </div>
      </PageContent>

      <Dialog
        open={pendingRemoval !== null}
        onClose={() => {
          setPendingRemoval(null)
        }}
        title="Remove from workspace"
        description={
          pendingRemoval === null
            ? undefined
            : `${memberName(pendingRemoval)} will lose access to ${workspace.name}. They can be invited back, but this cannot be undone from here.`
        }
        footer={
          <div className={shared.rowActions}>
            <Button
              onClick={() => {
                setPendingRemoval(null)
              }}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={isSubmitting}
              onClick={() => {
                const target = pendingRemoval

                if (target === null) {
                  return
                }

                setPendingRemoval(null)

                void removeMember(target.userId).then((outcome) => {
                  // The server refuses to remove the last owner, and also
                  // anyone still holding something shared -- a project lead,
                  // an initiative owner, a shared view, a connected
                  // integration. Each arrives as `rejected` carrying the
                  // reason and what to do about it. None of those rules is
                  // reimplemented on this side: a rule enforced in two places
                  // is a rule that will eventually disagree with itself. The
                  // screen's whole job is to say which person the reason is
                  // about, which the server has no way to know it should.
                  report(
                    outcome,
                    `${memberName(target)} was removed.`,
                    false,
                    `${memberName(target)} was not removed. `,
                  )
                })
              }}
            >
              Remove
            </Button>
          </div>
        }
      >
        <p>
          {pendingRemoval === null
            ? null
            : `Removing ${memberName(pendingRemoval)} does not delete the issues they created or were assigned.`}
        </p>
      </Dialog>
    </>
  )
}
