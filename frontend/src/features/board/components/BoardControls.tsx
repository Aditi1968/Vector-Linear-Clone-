import { useId } from 'react'

import { Button, Select, statusCategoryFrom } from '../../../components'
import type { WorkflowState, WorkspaceTeam } from '../../issues/api'
import { PRIORITY_VALUES, describePriority } from '../../issues/lib/priority'
import styles from '../board.module.css'
import { GROUP_BY, SORT_BY, clearFilters, hasActiveFilter } from '../lib/viewState'
import type { BoardView } from '../lib/viewState'
import type { BoardFilterOptions, FilterOption } from '../lib/arrange'

/** The visible name of each grouping and each ordering. */
const GROUP_LABELS: Record<BoardView['group'], string> = {
  status: 'Status',
  assignee: 'Assignee',
  priority: 'Priority',
  project: 'Project',
}

const SORT_LABELS: Record<BoardView['sort'], string> = {
  priority: 'Priority',
  created: 'Newest',
  updated: 'Recently updated',
  due: 'Due date',
}

/** What "no filter" is spelled as in a `<select>`: the empty string. */
const ANY = ''

export interface BoardControlsProps {
  view: BoardView
  teams: readonly WorkspaceTeam[]
  /** The selected team's states, which is where the status filter's options come from. */
  states: readonly WorkflowState[]
  options: BoardFilterOptions
  onChange: (next: BoardView) => void
}

/**
 * The filter, sort and grouping bar.
 *
 * Native `<select>` elements throughout, and that is the decision worth
 * defending: a listbox built out of divs would need its own keyboard model,
 * its own focus management and its own mobile behaviour, and would arrive
 * worse than what the platform ships. Every control here is labelled by a real
 * `<label for>` -- the accessible names are what the tests select by, and what
 * a screen-reader user hears before the value.
 *
 * ## The status filter offers categories, named by the team's own states
 *
 * `WorkflowStateCategory` is the fixed meaning behind a state, and it is what
 * a filter should be built on -- a team may call its started state anything.
 * But "STARTED" is not a word to put in a picker, and inventing a display name
 * for it here would fork the one `components/indicators` already owns. So each
 * option is named by the team's own states in that category: pick "In review,
 * Building" and you have picked the started category, spelled the way this
 * team spells it.
 */
export function BoardControls({
  view,
  teams,
  states,
  options,
  onChange,
}: BoardControlsProps) {
  const ids = useId()
  const id = (name: string) => `${ids}-${name}`

  /** One category per distinct category the team's states use, in board order. */
  const categories: FilterOption[] = []

  for (const state of [...states].sort((left, right) => left.position - right.position)) {
    const category = statusCategoryFrom(state.category)

    if (category === null) {
      continue
    }

    const existing = categories.find((candidate) => candidate.id === category)

    if (existing === undefined) {
      categories.push({ id: category, name: state.name })
    } else {
      existing.name = `${existing.name}, ${state.name}`
    }
  }

  /** Apply one field of the view, leaving the rest alone. */
  const update = <Field extends keyof BoardView>(field: Field, value: BoardView[Field]) => {
    onChange({ ...view, [field]: value })
  }

  /** A `<select>` of `{id, name}`, with an "any" option at the top. */
  const filterSelect = (
    field: 'assignee' | 'label' | 'project' | 'cycle',
    label: string,
    entries: readonly FilterOption[],
  ) => (
    <div className={styles.control}>
      <label className={styles.controlLabel} htmlFor={id(field)}>
        {label}
      </label>
      <Select
        disabled={entries.length === 0}
        id={id(field)}
        onChange={(event) => {
          update(field, event.target.value === ANY ? null : event.target.value)
        }}
        size="sm"
        value={view[field] ?? ANY}
      >
        <option value={ANY}>Any</option>
        {entries.map((entry) => (
          <option key={entry.id} value={entry.id}>
            {entry.name}
          </option>
        ))}
      </Select>
    </div>
  )

  return (
    /* A named group, so the whole bar is one thing to skip past rather than
     * nine unexplained pickers between the heading and the board. */
    <div aria-label="Board view" className={styles.controls} role="group">
      <div className={styles.control}>
        <label className={styles.controlLabel} htmlFor={id('team')}>
          Team
        </label>
        <Select
          id={id('team')}
          onChange={(event) => {
            update('team', event.target.value)
          }}
          size="sm"
          value={view.team ?? ANY}
        >
          {teams.map((team) => (
            <option key={team.id} value={team.key}>
              {team.key} · {team.name}
            </option>
          ))}
        </Select>
      </div>

      <div className={styles.control}>
        <label className={styles.controlLabel} htmlFor={id('group')}>
          Group by
        </label>
        <Select
          id={id('group')}
          onChange={(event) => {
            // Narrowed against the same list the URL is parsed against, so an
            // option that is not one of the four cannot enter the view.
            const next = GROUP_BY.find((candidate) => candidate === event.target.value)

            if (next !== undefined) {
              update('group', next)
            }
          }}
          size="sm"
          value={view.group}
        >
          {GROUP_BY.map((group) => (
            <option key={group} value={group}>
              {GROUP_LABELS[group]}
            </option>
          ))}
        </Select>
      </div>

      <div className={styles.control}>
        <label className={styles.controlLabel} htmlFor={id('sort')}>
          Sort by
        </label>
        <Select
          id={id('sort')}
          onChange={(event) => {
            const next = SORT_BY.find((candidate) => candidate === event.target.value)

            if (next !== undefined) {
              update('sort', next)
            }
          }}
          size="sm"
          value={view.sort}
        >
          {SORT_BY.map((sort) => (
            <option key={sort} value={sort}>
              {SORT_LABELS[sort]}
            </option>
          ))}
        </Select>
      </div>

      <div className={styles.control}>
        <label className={styles.controlLabel} htmlFor={id('status')}>
          Status
        </label>
        <Select
          disabled={categories.length === 0}
          id={id('status')}
          onChange={(event) => {
            update('status', statusCategoryFrom(event.target.value))
          }}
          size="sm"
          value={view.status ?? ANY}
        >
          <option value={ANY}>Any</option>
          {categories.map((category) => (
            <option key={category.id} value={category.id}>
              {category.name}
            </option>
          ))}
        </Select>
      </div>

      {filterSelect('assignee', 'Assignee', options.assignees)}
      {filterSelect('label', 'Label', options.labels)}

      <div className={styles.control}>
        <label className={styles.controlLabel} htmlFor={id('priority')}>
          Priority
        </label>
        <Select
          id={id('priority')}
          onChange={(event) => {
            const next = Number(event.target.value)

            update(
              'priority',
              event.target.value === ANY || !PRIORITY_VALUES.includes(next) ? null : next,
            )
          }}
          size="sm"
          value={view.priority === null ? ANY : String(view.priority)}
        >
          <option value={ANY}>Any</option>
          {PRIORITY_VALUES.map((priority) => {
            const { name } = describePriority(priority)

            return (
              <option key={priority} value={priority}>
                {name ?? priority}
              </option>
            )
          })}
        </Select>
      </div>

      {filterSelect('project', 'Project', options.projects)}
      {filterSelect('cycle', 'Cycle', options.cycles)}

      {/* Only when there is something to clear. A permanently visible "Clear
        * filters" on an unfiltered board is a control that does nothing. */}
      {hasActiveFilter(view) && (
        <Button
          className={styles.clear}
          onClick={() => {
            onChange(clearFilters(view))
          }}
          size="sm"
        >
          Clear filters
        </Button>
      )}
    </div>
  )
}
