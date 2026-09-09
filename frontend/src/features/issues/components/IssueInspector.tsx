import { useCallback, useId, useState } from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'

import { useAppPaths, useWorkspaceSlug } from '../../../app/routes'
import {
  Avatar,
  CloseIcon,
  IconButton,
  Menu,
  Select,
  Spinner,
  statusCategoryFrom,
  StatusIndicator,
  VisuallyHidden,
} from '../../../components'
import {
  CommentsPanel,
  LabelsPanel,
  RelationsPanel,
  SubIssuesPanel,
} from '../../collaboration'
import {
  groupByField,
  memberLabel,
  useIssueDetail,
  useIssueMutations,
  useTeamCycles,
  useWorkspaceContext,
} from '../api'
import type { IssueSaveOutcome } from '../api'
import styles from '../issues.module.css'
import { formatAbsolute, formatRelative } from '../lib/dates'
import { describePriority, PRIORITY_VALUES } from '../lib/priority'
import { EditableText } from './EditableText'
import { IssueDetailSkeleton, IssueLoadError } from './ListStates'

/** Stable identity for "nothing was rejected". */
const NO_ERRORS: ReadonlyMap<string, string[]> = new Map()

/**
 * The fields this panel renders a control for.
 *
 * A validation error naming one of these is shown against that control. An
 * error naming anything else -- which would mean the server's contract moved
 * -- is shown at the top of the panel rather than dropped on the floor.
 */
const PLACED_FIELDS = new Set([
  'title',
  'description',
  'priority',
  'workflowStateId',
  'assigneeId',
  'estimate',
  'dueDate',
  'projectId',
  'cycleId',
])

/** What a control needs in order to be labelled and to report its errors. */
interface ControlProps {
  id: string
  'aria-describedby': string | undefined
  'aria-invalid': true | undefined
}

interface PropertyProps {
  /** The server's own field name. Both the id seed and the error key. */
  name: string
  label: string
  baseId: string
  errors: ReadonlyMap<string, string[]>
  children: (control: ControlProps) => ReactNode
}

/**
 * One labelled row of the property panel.
 *
 * A render-child rather than a wrapper, because the label, the error message
 * and the control have to agree on three ids, and every arrangement where
 * the caller writes those ids by hand is one where a control eventually
 * ships without its `aria-describedby`. Here the wiring is impossible to
 * omit: the control cannot be rendered without receiving it.
 */
function Property({ name, label, baseId, errors, children }: PropertyProps) {
  const id = `${baseId}-${name}`
  const errorId = `${id}-error`
  const messages = errors.get(name) ?? []

  return (
    <div className={styles.property}>
      <label className={styles.propertyLabel} htmlFor={id}>
        {label}
      </label>
      <div className={styles.propertyControl}>
        {children({
          id,
          'aria-describedby': messages.length > 0 ? errorId : undefined,
          'aria-invalid': messages.length > 0 || undefined,
        })}
        {messages.length > 0 && (
          <p className={styles.fieldError} id={errorId}>
            {messages.join(' ')}
          </p>
        )}
      </div>
    </div>
  )
}

export interface IssueInspectorProps {
  /** The issue id from the route. The panel owns no selection state. */
  issueId: string
}

/**
 * One issue, editable in place, beside the list it came from.
 *
 * ## Two columns: the issue, and the readouts
 *
 * The issue column is a document -- breadcrumb, title, state, description,
 * activity -- and the second column is a stack of properties on the rail's
 * own sunken ground. They are grid tracks and not two components, so the
 * panel is one element that reflows: stacked in the 24rem pane, side by side
 * at 90rem where there is room for the design's third column. The list does
 * not step aside to make room for it, because the split pane *is* this
 * screen.
 *
 * ## Route-backed, which is what makes a refresh work
 *
 * The id arrives as a prop read from the URL, and the query runs from it.
 * No issue object is handed over from the list and there is no state that
 * only exists if the user got here by clicking a row -- so loading
 * `/:workspaceSlug/issues/<id>` directly, or pressing F5 on it, renders
 * exactly the same panel by exactly the same path. That is a property of
 * where the id comes from, not of this component, which is why the test for
 * it drives the router rather than this file.
 *
 * ## Every editable field is one the schema accepts
 *
 * Title, description, priority, workflow state, assignee, estimate and due
 * date go through `issueUpdate`. Project and cycle do not -- `IssueUpdateInput`
 * has neither, and the schema exposes `issueSetProject` and `issueSetCycle`
 * instead -- so those two controls call different mutations and are
 * otherwise indistinguishable to the user, which is the correct place for
 * that seam to be invisible.
 *
 * ## Errors land on the control that caused them
 *
 * `errors: [ValidationErrorType!]!` names a field. Grouping by that name and
 * handing each `Property` its own messages is what keeps a rejected estimate
 * from being reported as a banner at the top of the panel saying something
 * went wrong. The banner is reserved for the two things no field owns: a
 * transport failure, and an error naming a field this panel does not render.
 *
 * ## Not found is a state, not an error
 *
 * `issue(id:)` is nullable in the schema, so null is a successful answer
 * meaning the row is not there. It gets its own panel with a way back to the
 * list, rather than the red one a transport failure gets, because the two
 * need different things from the user.
 */
export function IssueInspector({ issueId }: IssueInspectorProps) {
  const navigate = useNavigate()
  const paths = useAppPaths()
  const workspaceSlug = useWorkspaceSlug()

  const { issue, isLoading, isNotFound, errorMessage, retry } = useIssueDetail(issueId)
  // `memberById` names the creator, who may well have left; `activeMembers`
  // is the assignee picker, which may only offer people who are still here.
  const { teamById, memberById, activeMembers, projects, isLoading: isContextLoading } =
    useWorkspaceContext()
  const cycles = useTeamCycles(issue?.teamId)
  const { updateIssue, setProject, setCycle, archiveIssue, isSaving } =
    useIssueMutations()

  const [fieldErrors, setFieldErrors] = useState(NO_ERRORS)
  const [panelError, setPanelError] = useState<string | null>(null)

  // `useId` rather than a constant: two inspectors on one page -- or one
  // rendered twice in a test -- would otherwise produce duplicate ids and
  // every label would point at the first copy's control.
  const baseId = useId()

  const close = useCallback(() => {
    void navigate(paths.issues())
  }, [navigate, paths])

  /**
   * Run one write and put its answer where it belongs.
   *
   * Every control goes through this, so there is exactly one place that
   * decides what a rejection looks like and exactly one place that clears
   * the previous attempt's messages.
   */
  const apply = useCallback(
    async (run: () => Promise<IssueSaveOutcome>) => {
      setFieldErrors(NO_ERRORS)
      setPanelError(null)

      const outcome = await run()

      if (outcome.status === 'rejected') {
        setFieldErrors(groupByField(outcome.errors))
        return
      }

      if (outcome.status === 'failed') {
        setPanelError(outcome.message)
      }
    },
    [],
  )

  const archive = useCallback(() => {
    void archiveIssue(issueId).then((failure) => {
      if (failure !== null) {
        setPanelError(failure)
        return
      }

      // The row is already gone from the cached list (see api/cache.ts).
      // Closing the panel is the whole of the feedback: the issue visibly
      // leaves the list beside it, which is more convincing than a toast
      // saying it did.
      close()
    })
  }, [archiveIssue, close, issueId])

  if (isLoading) {
    return <IssueDetailSkeleton />
  }

  if (errorMessage !== null) {
    return (
      <IssueLoadError
        message={errorMessage}
        onRetry={retry}
        title="Could not load this issue"
      />
    )
  }

  if (isNotFound || issue === null) {
    return (
      <div className={styles.inspectorMissing}>
        <h2 className={styles.missingHeading}>Issue not found</h2>
        <p className={styles.emptyBody}>
          No issue exists for this id. The link may be wrong or incomplete.
        </p>
        <button className={styles.backLink} onClick={close} type="button">
          Back to issues
        </button>
      </div>
    )
  }

  const team = teamById.get(issue.teamId)
  const states = team?.workflowStates ?? []
  const state = states.find((candidate) => candidate.id === issue.workflowStateId)
  const category = state === undefined ? null : statusCategoryFrom(state.category)
  const creator = issue.creatorId === null ? undefined : memberById.get(issue.creatorId)

  const unplaced = [...fieldErrors]
    .filter(([field]) => !PLACED_FIELDS.has(field))
    .flatMap(([field, messages]) => messages.map((message) => `${field}: ${message}`))

  /*
   * The activity log, newest first.
   *
   * ponytail: three timestamps, not an event stream. `createdAt`, `updatedAt`
   * and `completedAt` are all the schema has, and only the first of them names
   * a person -- so the middle entry says what happened and not who did it,
   * which is honest rather than a guess at an actor. An `IssueEvent`
   * connection would fill the same grid without changing its shape or this
   * component's markup.
   */
  const log = [
    { at: issue.updatedAt, who: undefined, what: 'last updated' },
    issue.completedAt === null
      ? null
      : { at: issue.completedAt, who: undefined, what: 'closed this issue' },
    { at: issue.createdAt, who: creator, what: 'created this issue' },
  ].filter((entry) => entry !== null)

  return (
    <article className={styles.inspector}>
      <div className={styles.issueColumn}>
        <header className={styles.inspectorHeader}>
          {/*
            The breadcrumb: where this issue lives, then what it is called. The
            team is a sibling of the heading rather than part of it, because
            the heading's accessible name is the key and only the key -- that is
            the name a person says out loud, and it is what the navigation test
            looks the panel up by.
          */}
          {team !== undefined && (
            <>
              <span className={styles.breadcrumbTeam}>{team.name}</span>
              <span aria-hidden="true" className={styles.breadcrumbSeparator}>
                /
              </span>
            </>
          )}

          {/*
            The identifier is the heading, not the title. A heading names a
            section and must stay stable while the thing it names is being
            edited -- and ENG-42 is the name this issue has outside the
            product. The title is a labelled field two lines below.
          */}
          <h2 className={styles.inspectorHeading}>{issue.identifier}</h2>

          {isSaving && <Spinner label="Saving" />}

          <Menu
            align="end"
            items={[
              {
                id: 'archive',
                label: 'Archive issue',
                destructive: true,
                onSelect: archive,
              },
            ]}
            label={`More actions on ${issue.identifier}`}
          />

          <IconButton
            aria-label="Close issue"
            icon={<CloseIcon />}
            onClick={close}
            size="sm"
            variant="ghost"
          />
        </header>

        <div className={styles.inspectorBody}>
          {panelError !== null && (
            <p className={styles.formError} role="alert">
              {panelError}
            </p>
          )}

          {unplaced.length > 0 && (
            <p className={styles.formError} role="alert">
              {unplaced.join(' ')}
            </p>
          )}

          <Property baseId={baseId} errors={fieldErrors} label="Title" name="title">
            {(control) => (
              <EditableText
                {...control}
                className={styles.titleField}
                onCommit={(title) => {
                  void apply(() => updateIssue(issue, { title }))
                }}
                value={issue.title}
              />
            )}
          </Property>

          {/*
            The state as a readout, under the title where the design puts it.
            The only status glyph in the panel -- it used to sit in the header
            beside the key -- so nothing is announced twice, and the Status
            select in the properties column is still the one control that
            changes it.
          */}
          {category !== null && (
            <div className={styles.chipRow}>
              <span className={styles.statusChip}>
                {/* `showLabel` and not the name written out beside it: the
                  * indicator is `role="img"` with the name on the wrapper, so
                  * its own visible text is inside that graphic and is
                  * announced once. A sibling `{state.name}` would be read a
                  * second time. */}
                <StatusIndicator category={category} name={state?.name} showLabel />
              </span>
            </div>
          )}

          <Property
            baseId={baseId}
            errors={fieldErrors}
            label="Description"
            name="description"
          >
            {(control) => (
              <EditableText
                {...control}
                multiline
                onCommit={(description) => {
                  // Whitespace-only means "no description", which is a
                  // decision about an empty box rather than a rewrite of
                  // content: any real text is sent unchanged.
                  void apply(() =>
                    updateIssue(issue, {
                      description: description.trim().length === 0 ? null : description,
                    }),
                  )
                }}
                placeholder="Add a description..."
                value={issue.description ?? ''}
              />
            )}
          </Property>

          {/*
            Agent A7's territory. Four self-contained panels, each rendering
            its own level-3 heading; this file supplies the slot and the two
            identifiers, and knows nothing else about them. See
            features/collaboration/index.ts for the contract.
          */}
          <LabelsPanel issueId={issue.id} workspaceSlug={workspaceSlug} />
          <SubIssuesPanel issueId={issue.id} workspaceSlug={workspaceSlug} />
          <RelationsPanel issueId={issue.id} workspaceSlug={workspaceSlug} />
          <CommentsPanel issueId={issue.id} workspaceSlug={workspaceSlug} />

          {/*
            The three timestamps, read as a log rather than as a definition
            list: when, who, what, one hairline per entry. `completedAt` only
            when the server has one -- it is derived from the workflow state's
            category, so an always-present "not completed" line would describe
            nothing.

            The person is announced but not drawn twice: the avatar is
            decorative and the name is in the sentence, visually hidden, so a
            screen reader hears "Ada Lovelace created this issue" while the
            column shows initials.
          */}
          <div className={styles.activity}>
            <h3 className={styles.activityHeading}>Activity</h3>

            {log.map((entry) => (
              <div className={styles.logEntry} key={entry.what}>
                <time
                  className={styles.logTime}
                  dateTime={entry.at}
                  title={formatAbsolute(entry.at)}
                >
                  {formatRelative(entry.at)}
                </time>

                {entry.who === undefined ? (
                  <span aria-hidden="true" />
                ) : (
                  <Avatar
                    className={styles.logWho}
                    decorative
                    name={memberLabel(entry.who)}
                    size="sm"
                  />
                )}

                <span className={styles.logWhat}>
                  {entry.who !== undefined && (
                    <VisuallyHidden>{memberLabel(entry.who)} </VisuallyHidden>
                  )}
                  {entry.what}
                </span>
              </div>
            ))}
          </div>

          <p className={styles.inspectorId}>
            <VisuallyHidden>Issue id</VisuallyHidden>
            {issue.id}
          </p>
        </div>
      </div>

      <div className={styles.propertiesColumn}>
        <div className={styles.properties}>
          <Property
            baseId={baseId}
            errors={fieldErrors}
            label="Status"
            name="workflowStateId"
          >
            {(control) => (
              <Select
                {...control}
                // Disabled rather than showing an empty list: until the
                // workspace context answers there are no states to choose
                // from, and a select whose value matches no option renders
                // blank and looks like data loss.
                disabled={states.length === 0}
                onChange={(event) => {
                  void apply(() =>
                    updateIssue(issue, { workflowStateId: event.target.value }),
                  )
                }}
                size="sm"
                value={issue.workflowStateId}
              >
                {states.length === 0 && (
                  <option value={issue.workflowStateId}>
                    {isContextLoading ? 'Loading...' : 'Unknown'}
                  </option>
                )}
                {states.map((option) => (
                  <option key={option.id} value={option.id}>
                    {option.name}
                  </option>
                ))}
              </Select>
            )}
          </Property>

          <Property baseId={baseId} errors={fieldErrors} label="Priority" name="priority">
            {(control) => (
              <Select
                {...control}
                onChange={(event) => {
                  void apply(() =>
                    updateIssue(issue, { priority: Number(event.target.value) }),
                  )
                }}
                size="sm"
                value={issue.priority}
              >
                {PRIORITY_VALUES.map((value) => {
                  const { name } = describePriority(value)

                  return (
                    <option key={value} value={value}>
                      {name ?? String(value)}
                    </option>
                  )
                })}
              </Select>
            )}
          </Property>

          <Property
            baseId={baseId}
            errors={fieldErrors}
            label="Assignee"
            name="assigneeId"
          >
            {(control) => (
              <Select
                {...control}
                onChange={(event) => {
                  // The empty string is the "nobody" option. Sent as an
                  // explicit null, which is what unassigns -- omitting the
                  // key would leave the current assignee in place.
                  const next = event.target.value

                  void apply(() =>
                    updateIssue(issue, { assigneeId: next === '' ? null : next }),
                  )
                }}
                size="sm"
                value={issue.assigneeId ?? ''}
              >
                <option value="">Unassigned</option>
                {activeMembers.map((member) => (
                  <option key={member.userId} value={member.userId}>
                    {memberLabel(member)}
                  </option>
                ))}
              </Select>
            )}
          </Property>

          <Property baseId={baseId} errors={fieldErrors} label="Estimate" name="estimate">
            {(control) => (
              <EditableText
                {...control}
                onCommit={(next) => {
                  const trimmed = next.trim()
                  const parsed = Number(trimmed)

                  // A blank box clears the estimate. Anything that is not a
                  // number is left to the field rather than sent as NaN,
                  // which would fail at variable coercion as a top-level
                  // error instead of as a rejected value.
                  if (trimmed !== '' && !Number.isInteger(parsed)) {
                    return
                  }

                  void apply(() =>
                    updateIssue(issue, { estimate: trimmed === '' ? null : parsed }),
                  )
                }}
                type="number"
                value={issue.estimate === null ? '' : String(issue.estimate)}
              />
            )}
          </Property>

          <Property baseId={baseId} errors={fieldErrors} label="Due date" name="dueDate">
            {(control) => (
              <EditableText
                {...control}
                onCommit={(next) => {
                  // `type="date"` hands back exactly `YYYY-MM-DD`, which is
                  // the `Date` scalar's wire format, and the empty string
                  // when the field is cleared.
                  void apply(() =>
                    updateIssue(issue, { dueDate: next === '' ? null : next }),
                  )
                }}
                type="date"
                value={issue.dueDate ?? ''}
              />
            )}
          </Property>

          <Property baseId={baseId} errors={fieldErrors} label="Project" name="projectId">
            {(control) => (
              <Select
                {...control}
                onChange={(event) => {
                  const next = event.target.value

                  void apply(() => setProject(issue.id, next === '' ? null : next))
                }}
                size="sm"
                value={issue.project?.id ?? ''}
              >
                <option value="">No project</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))}
              </Select>
            )}
          </Property>

          <Property baseId={baseId} errors={fieldErrors} label="Cycle" name="cycleId">
            {(control) => (
              <Select
                {...control}
                onChange={(event) => {
                  const next = event.target.value

                  void apply(() => setCycle(issue.id, next === '' ? null : next))
                }}
                size="sm"
                value={issue.cycle?.id ?? ''}
              >
                <option value="">No cycle</option>
                {cycles.map((cycle) => (
                  <option key={cycle.id} value={cycle.id}>
                    {cycle.name ?? `Cycle ${String(cycle.number)}`}
                  </option>
                ))}
              </Select>
            )}
          </Property>
        </div>
      </div>
    </article>
  )
}
