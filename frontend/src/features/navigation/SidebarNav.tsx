import { useState } from 'react'
import { useParams } from 'react-router-dom'
import { useQuery } from '@apollo/client/react'

import { ChevronRightIcon, Skeleton, VisuallyHidden, cx } from '../../components'
import { ShellSidebarDocument } from '../../generated/operations'
import type { ShellSidebarQuery } from '../../generated/operations'
import { TEAM_KEY_PARAM, useAppPaths, useWorkspaceSlug } from '../../app/routes'
import type { AppPaths } from '../../app/routes'
import { NavRow } from './NavRow'
import { primaryNavigationItems, workspaceNavigationItems } from './navigationItems'
import styles from './SidebarNav.module.css'

type Team = ShellSidebarQuery['teams'][number]

export interface SidebarNavProps {
  /** The id the rail's collapse toggle names in `aria-controls`. */
  id: string
  collapsed: boolean
}

/**
 * The rail's navigation.
 *
 * One `<nav>` with a label, so it is a navigation landmark a screen-reader
 * user can jump straight to. Inside it, three `<ul>`s -- the product's
 * surfaces, the teams, and the workspace itself -- each with its own
 * `aria-label`, so a reader hears "Teams, list, 3 items" rather than three
 * anonymous runs of links. `role="list"` is stated explicitly because Safari
 * drops the implicit list role when `list-style: none` is applied, which this
 * stylesheet does.
 *
 * Three lists in one landmark rather than three landmarks: a rail with four
 * `navigation` regions in it is landmark noise, and the sections are visually
 * one navigation region because that is what they are.
 *
 * ## The data
 *
 * `ShellSidebar` supplies both the teams and the Inbox count, in one request.
 * The teams are the workspace's real ones from `teams(workspaceSlug:)` -- no
 * fixture, no default team, no "Engineering" invented here -- and the count
 * is `notificationUnreadCount`, so the badge is a real number of unread
 * notifications or it is not rendered at all.
 */
export function SidebarNav({ id, collapsed }: SidebarNavProps) {
  const paths = useAppPaths()
  const workspaceSlug = useWorkspaceSlug()

  const { data, loading, error } = useQuery(ShellSidebarDocument, {
    variables: { workspaceSlug },
  })

  const unread = data?.notificationUnreadCount ?? 0

  return (
    <nav id={id} className={styles.nav} aria-label="Main">
      <ul className={styles.list} role="list" aria-label="Workspace">
        {primaryNavigationItems.map((item) => (
          <li key={item.id}>
            <NavRow
              to={item.to(paths)}
              end={item.end ?? false}
              icon={item.icon}
              label={item.label}
              badge={
                item.id === 'inbox' && unread > 0 ? (
                  <span className={styles.badge}>
                    {unread}
                    <VisuallyHidden> unread</VisuallyHidden>
                  </span>
                ) : undefined
              }
            />
          </li>
        ))}
      </ul>

      <TeamsSection
        paths={paths}
        teams={data?.teams}
        loading={loading}
        failed={error !== undefined}
        collapsed={collapsed}
      />

      <ul className={styles.list} role="list" aria-label="Administration">
        {workspaceNavigationItems.map((item) => (
          <li key={item.id}>
            <NavRow
              to={item.to(paths)}
              end={item.end ?? false}
              icon={item.icon}
              label={item.label}
            />
          </li>
        ))}
      </ul>
    </nav>
  )
}

interface TeamsSectionProps {
  paths: AppPaths
  teams: readonly Team[] | undefined
  loading: boolean
  failed: boolean
  collapsed: boolean
}

/**
 * The workspace's teams, each expandable to its own sections.
 *
 * ## Disclosure state
 *
 * A plain `<button>` with `aria-expanded` and `aria-controls`, not a
 * `<details>`: `<details open={...}>` is uncontrolled in the DOM and
 * controlled in React at the same time, and reconciling the two costs more
 * than the eleven lines below.
 *
 * Open by default for the team the URL is inside, and remembered per team
 * once the user touches it. `expanded[key] ?? key === activeTeamKey` does
 * both with one piece of state and no effect -- an effect that seeded the map
 * from the route would fight the user every time they collapsed the team they
 * are currently in.
 *
 * The sub-list is always in the DOM and hidden with the `hidden` attribute
 * rather than unmounted, so `aria-controls` always names an element that
 * exists.
 *
 * ## States
 *
 * Skeleton rows while loading, because this sits inside chrome that is
 * already on screen and a spinner in a sidebar reads as an error. A failure
 * says so quietly and does not offer a retry: the rail is not where a user
 * should be recovering from a network problem, and the page beside it has its
 * own error state with its own retry. A workspace with no teams says so --
 * that is a real state for a new workspace, not a failure.
 */
function TeamsSection({ paths, teams, loading, failed, collapsed }: TeamsSectionProps) {
  const activeTeamKey = useParams()[TEAM_KEY_PARAM]
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  return (
    <div className={styles.section}>
      <p className={cx(styles.sectionLabel, styles.collapsible)} id="nav-teams-label">
        Teams
      </p>

      {loading && (
        <div className={styles.teamsLoading} aria-hidden="true">
          <Skeleton height="1.5rem" width="80%" />
          <Skeleton height="1.5rem" width="65%" />
        </div>
      )}

      {!loading && failed && (
        <p className={cx(styles.sectionNote, styles.collapsible)}>
          Teams unavailable
        </p>
      )}

      {!loading && !failed && teams?.length === 0 && (
        <p className={cx(styles.sectionNote, styles.collapsible)}>No teams yet</p>
      )}

      {teams !== undefined && teams.length > 0 && (
        <ul className={styles.list} role="list" aria-labelledby="nav-teams-label">
          {teams.map((team) => {
            const open = expanded[team.key] ?? team.key === activeTeamKey
            const panelId = `nav-team-${team.id}`

            return (
              <li key={team.id}>
                <div className={styles.teamRow}>
                  <button
                    type="button"
                    className={styles.disclosure}
                    aria-expanded={open}
                    aria-controls={panelId}
                    aria-label={`${open ? 'Collapse' : 'Expand'} ${team.name}`}
                    onClick={() => {
                      setExpanded((current) => ({ ...current, [team.key]: !open }))
                    }}
                  >
                    <ChevronRightIcon
                      className={cx(styles.chevron, open && styles.chevronOpen)}
                    />
                  </button>

                  {/* The team key sits in the icon column, which is what makes
                    * the rail readable when it is collapsed to glyphs: ENG is
                    * a better icon for a team than any drawing of one. */}
                  <NavRow
                    to={paths.team(team.key)}
                    end
                    icon={<span className={styles.teamKey}>{team.key}</span>}
                    label={team.name}
                    className={styles.teamLink}
                  />
                </div>

                <ul
                  id={panelId}
                  role="list"
                  className={styles.subList}
                  hidden={!open || collapsed}
                >
                  <li>
                    <NavRow nested to={paths.teamIssues(team.key)} label="Issues" />
                  </li>
                  <li>
                    <NavRow nested to={paths.cycles(team.key)} label="Cycles" />
                  </li>
                </ul>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
