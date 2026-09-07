import { useMemo } from 'react'
import { Link } from 'react-router-dom'

import { useAppPaths } from '../../../app/routes'
import { IssueRow, Menu, VisuallyHidden } from '../../../components'
import type { MenuItem } from '../../../components'
import type { WorkflowState, WorkspaceMember, WorkspaceTeam } from '../../issues/api'
import { formatRelative } from '../../issues/lib/dates'
import { PRIORITY_VALUES, describePriority, priorityLevel } from '../../issues/lib/priority'
import type { TriageRow as TriageRowData } from '../api'
import styles from '../triage.module.css'

export interface TriageRowProps {
  row: TriageRowData
  /** The current team's board. Each state is somewhere the issue can be accepted. */
  states: readonly WorkflowState[]
  /** The other teams, for handing the issue on. Excludes the queue's own team. */
  otherTeams: readonly WorkspaceTeam[]
  members: readonly WorkspaceMember[]
  /** One instant for the whole list, so every age is measured against the same now. */
  now: number
  /** A write is in flight somewhere on the screen. */
  isSaving: boolean
  onAccept: (issueId: string, workflowStateId: string) => void
  onDecline: (issueId: string) => void
  onChangeTeam: (issueId: string, teamId: string) => void
  onSetPriority: (issueId: string, priority: number) => void
  onAssign: (issueId: string, assigneeId: string | null) => void
  /** Opens the duplicate dialog, which needs a second issue to point at. */
  onMarkDuplicate: (issueId: string) => void
}

/**
 * One issue waiting to be triaged.
 *
 * ## What this row can and cannot show
 *
 * `TriageIssue.issue` is an `IssueSummary`, which carries `id`,
 * `identifier`, `title`, `description` and `priority` and nothing else. There
 * is no `assigneeId`, no `workflowStateId`, no `labels` and no `dueDate` on
 * it, so this row draws a priority glyph, a key, a title and an age -- and
 * draws no status glyph and no avatar, because it has no way to know them.
 *
 * That is a schema fact rather than a design choice, and it is deliberately
 * not papered over: a row rendering "Unassigned" because the field was
 * absent would be stating something it does not know. `IssueRow` leaves the
 * status column empty when `status` is omitted, which is the honest
 * rendering.
 *
 * The action menus can still *set* an assignee and a priority. Writing does
 * not require reading the current value first, so triage can do its job --
 * decide -- without being able to browse state.
 *
 * ## Why two menus and not one
 *
 * Accepting is the queue's purpose and every other action is an exception to
 * it, so accepting gets a named trigger of its own and the rest share an
 * overflow. It also keeps either menu short enough to read: a single menu
 * carrying every state, every other team, five priorities and every member
 * is a scrolling list, not a choice.
 */
export function TriageRow({
  row,
  states,
  otherTeams,
  members,
  now,
  isSaving,
  onAccept,
  onDecline,
  onChangeTeam,
  onSetPriority,
  onAssign,
  onMarkDuplicate,
}: TriageRowProps) {
  const paths = useAppPaths()
  const { issue } = row
  const priority = describePriority(issue.priority)

  /*
    Accepting means choosing the state the issue lands in. The schema requires
    `workflowStateId` and offers no default, which is correct: "accepted" is
    not a state of its own, it is whichever column of that team's board the
    person picked. Every state is offered rather than a guessed subset -- a
    team that triages straight into "In progress" is not doing it wrong.
  */
  const acceptItems = useMemo<MenuItem[]>(
    () =>
      states.map((state) => ({
        id: state.id,
        label: state.name,
        disabled: isSaving,
        onSelect: () => {
          onAccept(issue.id, state.id)
        },
      })),
    [isSaving, issue.id, onAccept, states],
  )

  const moreItems = useMemo<MenuItem[]>(() => {
    const items: MenuItem[] = [
      {
        id: 'decline',
        label: 'Decline',
        destructive: true,
        disabled: isSaving,
        onSelect: () => {
          onDecline(issue.id)
        },
      },
      {
        id: 'duplicate',
        // The ellipsis is a promise that a dialog follows: this action needs
        // a second issue to point at, which a menu cannot collect.
        label: 'Mark as duplicate...',
        disabled: isSaving,
        onSelect: () => {
          onMarkDuplicate(issue.id)
        },
      },
    ]

    for (const [index, value] of PRIORITY_VALUES.entries()) {
      items.push({
        id: `priority-${String(value)}`,
        label: describePriority(value).name ?? `Priority ${String(value)}`,
        separatorBefore: index === 0,
        disabled: isSaving || value === issue.priority,
        onSelect: () => {
          onSetPriority(issue.id, value)
        },
      })
    }

    for (const [index, member] of members.entries()) {
      items.push({
        id: `assign-${member.userId}`,
        label: member.name ?? member.email,
        separatorBefore: index === 0,
        disabled: isSaving,
        onSelect: () => {
          onAssign(issue.id, member.userId)
        },
      })
    }

    /*
      Offered unconditionally, unlike the priority items which disable the
      value already set: an `IssueSummary` does not carry `assigneeId`, so
      this row genuinely does not know whether the issue has an assignee to
      clear. Disabling it on a guess would be worse than a no-op.
    */
    items.push({
      id: 'unassign',
      label: 'Unassign',
      separatorBefore: members.length === 0,
      disabled: isSaving,
      onSelect: () => {
        onAssign(issue.id, null)
      },
    })

    for (const [index, team] of otherTeams.entries()) {
      items.push({
        id: `team-${team.id}`,
        label: `Move to ${team.key}`,
        separatorBefore: index === 0,
        disabled: isSaving,
        onSelect: () => {
          onChangeTeam(issue.id, team.id)
        },
      })
    }

    return items
  }, [
    isSaving,
    issue.id,
    issue.priority,
    members,
    onAssign,
    onChangeTeam,
    onDecline,
    onMarkDuplicate,
    onSetPriority,
    otherTeams,
  ])

  return (
    <IssueRow
      identifier={issue.identifier}
      priority={priorityLevel(issue.priority)}
      priorityName={priority.name ?? undefined}
      title={
        <Link className={styles.rowLink} to={paths.issue(issue.id)}>
          {issue.title}
        </Link>
      }
      meta={
        <>
          {/* A `<time>` element, because the text is relative and the
              machine-readable instant is the fact underneath it. */}
          <time className={styles.age} dateTime={row.enteredAt}>
            {formatRelative(row.enteredAt, now)}
          </time>

          <span className={styles.rowActions}>
            <Menu
              align="end"
              items={acceptItems}
              label={`Accept ${issue.identifier} into a state`}
              size="sm"
            >
              {/*
                `Menu` uses `label` as the trigger's `aria-label` only when
                the trigger is icon-only; with visible text the name is that
                text. Every row would then have a button called exactly
                "Accept", which is a list of identical names to anyone
                navigating by them. The identifier is appended for the
                accessible name and hidden from the visible one, so the
                column still reads "Accept" and the button is still unique.
              */}
              Accept{' '}
              <VisuallyHidden>{issue.identifier}</VisuallyHidden>
            </Menu>
            <Menu
              align="end"
              items={moreItems}
              label={`More actions on ${issue.identifier}`}
              size="sm"
            />
          </span>
        </>
      }
    />
  )
}
