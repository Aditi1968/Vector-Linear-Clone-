import { useCallback } from 'react'

import { Menu, PlusIcon, Tag } from '../../../components'
import type { MenuItem } from '../../../components'
import type { ProjectTeam } from '../api'
import { availableTeams, resolveTeams } from '../lib/projects'
import styles from '../projects.module.css'

export interface ProjectTeamsProps {
  /** `Project.teamIds` -- bare UUIDs, and plural on purpose. */
  teamIds: readonly string[]
  /** Every team in the workspace, for naming and for the picker. */
  teams: readonly ProjectTeam[]
  isSaving: boolean
  onAdd: (teamId: string) => void
  onRemove: (teamId: string) => void
}

/**
 * The teams a project spans.
 *
 * Plural, and that is the product concept rather than a shape this component
 * happened to be given: `Project.teamIds` is a list, `projectTeamAdd` and
 * `projectTeamRemove` are separate mutations, and a project with three teams
 * on it is ordinary. Nothing here has a notion of "the" team.
 *
 * A team id the workspace's team list does not contain is rendered as a
 * membership the viewer cannot resolve, not dropped. Dropping it would show
 * a two-team project as a one-team project -- a quiet misstatement of what
 * the project is, in the direction that hides work.
 */
export function ProjectTeams({
  teamIds,
  teams,
  isSaving,
  onAdd,
  onRemove,
}: ProjectTeamsProps) {
  const memberships = resolveTeams(teamIds, teams)
  const addable = availableTeams(teamIds, teams)

  const items: readonly MenuItem[] = addable.map((team) => ({
    id: team.id,
    label: `${team.key} · ${team.name}`,
    disabled: isSaving,
    onSelect: () => {
      onAdd(team.id)
    },
  }))

  const handleRemove = useCallback(
    (teamId: string) => () => {
      onRemove(teamId)
    },
    [onRemove],
  )

  return (
    <div className={styles.tagRow}>
      {memberships.map(({ teamId, team }) =>
        team === null ? (
          // Named for what it is. "Team not visible" is a fact about the
          // viewer's access; a UUID here would be a database key shown to a
          // person, and a blank would be a lie about the team count.
          <Tag key={teamId} name="Team not visible" onRemove={handleRemove(teamId)} />
        ) : (
          <Tag key={teamId} name={team.key} onRemove={handleRemove(teamId)} />
        ),
      )}

      {memberships.length === 0 && (
        <p className={styles.factEmpty}>No teams yet</p>
      )}

      {/* Absent, not disabled, when every team is already on the project: a
          disabled control says "you could do this, but not now", which is a
          different and false statement about a menu with nothing in it. */}
      {items.length > 0 && (
        <Menu label="Add a team" items={items} icon={<PlusIcon />} size="sm" align="start">
          Add team
        </Menu>
      )}
    </div>
  )
}
