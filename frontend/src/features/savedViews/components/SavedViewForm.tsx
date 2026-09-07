import { useId, useMemo, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, Input, Select } from '../../../components'
import type { WorkspaceTeam } from '../../issues/api'
import { PRIORITY_VALUES, describePriority } from '../../issues/lib/priority'
import type {
  IssueFilterInput,
  IssueOrderField,
  OrderDirection,
  SavedViewDraft,
  SavedViewFilter,
  SavedViewGrouping,
  SavedViewLayout,
  SavedViewValidationError,
  SavedViewVisibility,
  WorkflowStateCategory,
} from '../api'
import { GROUPING_VALUES, groupingLabel, toFilterInput } from '../lib/savedViews'
import styles from '../savedViews.module.css'

/** The five categories a workflow state can belong to, as the schema names them. */
const STATE_CATEGORIES: readonly WorkflowStateCategory[] = [
  'BACKLOG',
  'UNSTARTED',
  'STARTED',
  'COMPLETED',
  'CANCELED',
]

const CATEGORY_LABELS: Record<WorkflowStateCategory, string> = {
  BACKLOG: 'Backlog',
  UNSTARTED: 'Todo',
  STARTED: 'In progress',
  COMPLETED: 'Done',
  CANCELED: 'Canceled',
}

const ORDER_FIELDS: readonly IssueOrderField[] = [
  'PRIORITY',
  'CREATED_AT',
  'UPDATED_AT',
  'DUE_DATE',
]

const ORDER_FIELD_LABELS: Record<IssueOrderField, string> = {
  PRIORITY: 'Priority',
  CREATED_AT: 'Created',
  UPDATED_AT: 'Updated',
  DUE_DATE: 'Due date',
}

export interface SavedViewFormProps {
  /** The view being edited, or null when composing a new one. */
  initialName?: string
  initialTeamId?: string | null
  initialVisibility?: SavedViewVisibility
  initialLayout?: SavedViewLayout
  initialGrouping?: SavedViewGrouping | null
  initialOrderField?: IssueOrderField
  initialOrderDirection?: OrderDirection
  /**
   * The stored filter, when editing.
   *
   * Two jobs: it seeds the three filter controls below, and every field it
   * holds that this form does NOT render is carried back out unchanged. See
   * `buildFilter`.
   */
  initialFilter?: SavedViewFilter | null
  teams: readonly WorkspaceTeam[]
  submitLabel: string
  isSaving: boolean
  errors: readonly SavedViewValidationError[]
  errorMessage: string | null
  onCancel: () => void
  /**
   * `filter` is undefined when it must not be rewritten -- the caller sends a
   * patch without it, and `SavedViewUpdateInput` leaves the stored value
   * alone. See ../lib/savedViews.ts.
   */
  onSubmit: (draft: SavedViewDraft) => void
}

/**
 * The saved-view composer and editor.
 *
 * ## Which filters this form edits, and why not all of them
 *
 * `IssueFilterInput` has eight columns. This renders three -- team, status
 * category and priority -- because those three are the ones whose options are
 * already on screen or are a fixed enum. The other five (`workflowStateId`,
 * `labelId`, `assigneeId`, `projectId`, `cycleId`) each need a picker fed by
 * its own list, and a form that fetched four more documents to draw five more
 * selects is a bigger thing than this pass is.
 *
 * The five it does not render are *preserved*, not dropped: `buildFilter`
 * starts from the stored filter and overrides only the three controls. A
 * saved view filtered to one project keeps its project when someone renames
 * it here. That is the difference between a partial editor and a lossy one.
 *
 * ## When the filter cannot be edited at all
 *
 * `isFilterRewritable` is false when the stored filter says "unassigned", "in
 * no project" or "in no cycle" -- states `SavedViewFilter` can express and
 * `IssueFilterInput` cannot. In that case the caller passes no
 * `initialFilter` controls at all and the fields are disabled with the reason
 * shown, because sending a filter that silently widened the view would be
 * worse than not offering to.
 */
export function SavedViewForm({
  initialName = '',
  initialTeamId = null,
  initialVisibility = 'PERSONAL',
  initialLayout = 'LIST',
  initialGrouping = null,
  initialOrderField = 'CREATED_AT',
  initialOrderDirection = 'DESC',
  initialFilter = null,
  teams,
  submitLabel,
  isSaving,
  errors,
  errorMessage,
  onCancel,
  onSubmit,
}: SavedViewFormProps) {
  const fieldId = useId()

  const [name, setName] = useState(initialName)
  const [teamId, setTeamId] = useState(initialTeamId ?? '')
  const [visibility, setVisibility] = useState<SavedViewVisibility>(initialVisibility)
  const [layout, setLayout] = useState<SavedViewLayout>(initialLayout)
  const [grouping, setGrouping] = useState(initialGrouping ?? '')
  const [orderField, setOrderField] = useState<IssueOrderField>(initialOrderField)
  const [orderDirection, setOrderDirection] =
    useState<OrderDirection>(initialOrderDirection)

  const [filterTeamId, setFilterTeamId] = useState(initialFilter?.teamId ?? '')
  const [filterCategory, setFilterCategory] = useState(
    initialFilter?.stateCategory ?? '',
  )
  const [filterPriority, setFilterPriority] = useState(
    initialFilter?.priority === null || initialFilter?.priority === undefined
      ? ''
      : String(initialFilter.priority),
  )

  /*
    A stored filter this form must not rewrite. Null `initialFilter` on a new
    view is not the same thing -- there is nothing to lose -- so the check is
    for a filter that exists and cannot round-trip.
  */
  const preserved = initialFilter === null ? null : toFilterInput(initialFilter)
  const isFilterLocked = initialFilter !== null && preserved === null

  /** Errors the server named, by the field it named them on. */
  const errorByField = useMemo(() => {
    const map = new Map<string, string>()

    for (const entry of errors) {
      if (!map.has(entry.field)) {
        map.set(entry.field, entry.message)
      }
    }

    return map
  }, [errors])

  function buildFilter(): IssueFilterInput | undefined {
    // Omitted from the patch entirely, so `SavedViewUpdateInput` leaves the
    // stored filter exactly as it is.
    if (isFilterLocked) {
      return undefined
    }

    return {
      // Everything the form does not render, carried straight back.
      ...(preserved ?? {}),
      teamId: filterTeamId === '' ? null : filterTeamId,
      stateCategory: filterCategory === '' ? null : (filterCategory as WorkflowStateCategory),
      priority: filterPriority === '' ? null : Number(filterPriority),
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    const filter = buildFilter()

    onSubmit({
      name: name.trim(),
      teamId: teamId === '' ? null : teamId,
      visibility,
      layout,
      grouping: grouping === '' ? null : (grouping as SavedViewGrouping),
      // Not rendered and not invented: the schema has `subgrouping`, this
      // form does not offer it, and omitting it leaves it alone.
      orderBy: { field: orderField, direction: orderDirection },

      /*
        Spread-or-nothing rather than `filter: buildFilter()`. A key present
        with the value `undefined` is still a key, and it would travel to the
        server as an explicit `filter: null` -- clearing the stored filter,
        which is the precise outcome the lock exists to prevent. Omitting the
        key is what makes `SavedViewUpdateInput` leave it alone.
      */
      ...(filter === undefined ? {} : { filter }),
    })
  }

  const nameError = errorByField.get('name')

  return (
    <form className={styles.form} noValidate onSubmit={handleSubmit}>
      {errorMessage !== null && (
        <p className={styles.formError} role="alert">
          {errorMessage}
        </p>
      )}

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`${fieldId}-name`}>
          Name
        </label>
        <Input
          aria-describedby={nameError === undefined ? undefined : `${fieldId}-name-error`}
          aria-invalid={nameError === undefined ? undefined : true}
          id={`${fieldId}-name`}
          onChange={(event) => {
            setName(event.target.value)
          }}
          required
          value={name}
        />
        {nameError !== undefined && (
          <p className={styles.fieldError} id={`${fieldId}-name-error`}>
            {nameError}
          </p>
        )}
      </div>

      <div className={styles.fieldRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-team`}>
            Team
          </label>
          <Select
            id={`${fieldId}-team`}
            onChange={(event) => {
              setTeamId(event.target.value)
            }}
            value={teamId}
          >
            {/* A view with no team is workspace-wide, not malformed. */}
            <option value="">Whole workspace</option>
            {teams.map((team) => (
              <option key={team.id} value={team.id}>
                {team.key} · {team.name}
              </option>
            ))}
          </Select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-visibility`}>
            Visibility
          </label>
          <Select
            id={`${fieldId}-visibility`}
            onChange={(event) => {
              setVisibility(event.target.value as SavedViewVisibility)
            }}
            value={visibility}
          >
            <option value="PERSONAL">Personal — only you</option>
            <option value="SHARED">Shared — everyone in the workspace</option>
          </Select>
        </div>
      </div>

      <div className={styles.fieldRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-layout`}>
            Layout
          </label>
          <Select
            id={`${fieldId}-layout`}
            onChange={(event) => {
              setLayout(event.target.value as SavedViewLayout)
            }}
            value={layout}
          >
            <option value="LIST">List</option>
            <option value="BOARD">Board</option>
          </Select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-grouping`}>
            Group by
          </label>
          <Select
            id={`${fieldId}-grouping`}
            onChange={(event) => {
              setGrouping(event.target.value)
            }}
            value={grouping}
          >
            <option value="">No grouping</option>
            {GROUPING_VALUES.map((value) => (
              <option key={value} value={value}>
                {groupingLabel(value)}
              </option>
            ))}
          </Select>
        </div>
      </div>

      <div className={styles.fieldRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-order-field`}>
            Sort by
          </label>
          <Select
            id={`${fieldId}-order-field`}
            onChange={(event) => {
              setOrderField(event.target.value as IssueOrderField)
            }}
            value={orderField}
          >
            {ORDER_FIELDS.map((value) => (
              <option key={value} value={value}>
                {ORDER_FIELD_LABELS[value]}
              </option>
            ))}
          </Select>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-order-direction`}>
            Direction
          </label>
          <Select
            id={`${fieldId}-order-direction`}
            onChange={(event) => {
              setOrderDirection(event.target.value as OrderDirection)
            }}
            value={orderDirection}
          >
            <option value="ASC">Ascending</option>
            <option value="DESC">Descending</option>
          </Select>
        </div>
      </div>

      <fieldset className={styles.filterSet} disabled={isFilterLocked}>
        <legend className={styles.legend}>Filter</legend>

        {isFilterLocked && (
          /*
            Stated outright rather than hidden. This view filters on
            "unassigned", "in no project" or "in no cycle" -- meanings
            `SavedViewFilter` carries and `IssueFilterInput` cannot express --
            so rewriting the filter here would quietly widen the view. The
            rest of the form still saves.
          */
          <p className={styles.hint} role="note">
            This view filters on an empty column — unassigned, no project or no
            cycle. The API has no way to send that back, so the filter is kept
            exactly as it is and cannot be edited here. Everything else on this
            form still saves.
          </p>
        )}

        <div className={styles.fieldRow}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${fieldId}-filter-team`}>
              Filter by team
            </label>
            <Select
              id={`${fieldId}-filter-team`}
              onChange={(event) => {
                setFilterTeamId(event.target.value)
              }}
              value={filterTeamId}
            >
              <option value="">Any team</option>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>
                  {team.key} · {team.name}
                </option>
              ))}
            </Select>
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${fieldId}-filter-category`}>
              Filter by status
            </label>
            <Select
              id={`${fieldId}-filter-category`}
              onChange={(event) => {
                setFilterCategory(event.target.value)
              }}
              value={filterCategory}
            >
              <option value="">Any status</option>
              {STATE_CATEGORIES.map((value) => (
                <option key={value} value={value}>
                  {CATEGORY_LABELS[value]}
                </option>
              ))}
            </Select>
          </div>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fieldId}-filter-priority`}>
            Priority
          </label>
          <Select
            id={`${fieldId}-filter-priority`}
            onChange={(event) => {
              setFilterPriority(event.target.value)
            }}
            value={filterPriority}
          >
            <option value="">Any priority</option>
            {PRIORITY_VALUES.map((value) => (
              <option key={value} value={String(value)}>
                {describePriority(value).name ?? `Priority ${String(value)}`}
              </option>
            ))}
          </Select>
        </div>

        {/*
          The five columns this form does not draw a control for. Said out
          loud so that someone editing a view built elsewhere knows their
          project filter survived rather than wondering whether it was lost.
        */}
        <p className={styles.hint}>
          Filters on a specific state, label, assignee, project or cycle are
          kept as they are — this form does not edit them.
        </p>
      </fieldset>

      <div className={styles.formActions}>
        <Button onClick={onCancel} type="button">
          Cancel
        </Button>
        <Button disabled={isSaving || name.trim() === ''} type="submit" variant="primary">
          {submitLabel}
        </Button>
      </div>
    </form>
  )
}
