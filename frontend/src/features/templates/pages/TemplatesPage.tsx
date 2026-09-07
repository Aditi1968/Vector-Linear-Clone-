import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import { useAppPaths } from '../../../app/routes'
import {
  Badge,
  Button,
  Dialog,
  DocumentIcon,
  EmptyState,
  ErrorState,
  Input,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  Menu,
  Select,
  Skeleton,
  VisuallyHidden,
} from '../../../components'

import { useWorkspaceContext } from '../../issues/api'
import { describePriority } from '../../issues/lib/priority'
import { useTemplateActions, useTemplateList } from '../api'
import type { CreatedIssue, IssueTemplate, IssueTemplateDraft, TemplateValidationError } from '../api'
import { TemplateForm } from '../components/TemplateForm'
import styles from '../templates.module.css'

const NO_ERRORS: readonly TemplateValidationError[] = []

/** Templates with no team, then each team's, so the shared ones read first. */
interface TemplateGroup {
  key: string
  title: string
  templates: readonly IssueTemplate[]
}

/**
 * Issue templates: pre-filled issues, so recurring work is filed the same way.
 *
 * ## Why filing always asks for a team
 *
 * `IssueCreateFromTemplateInput.teamId` is non-null, and a template's own
 * `teamId` is nullable -- a template with none is workspace-wide. An issue
 * has to land in exactly one team, so the dialog asks; where the template
 * names a team it is preselected, and where it does not there is nothing to
 * guess from and the field starts empty.
 *
 * ## Why the editor carries fields it does not draw
 *
 * `IssueTemplateUpdateInput.template` is a whole-row replace. See
 * ../components/TemplateForm.tsx -- the four fields with no control
 * (`assigneeId`, `projectId`, `cycleId`, `labelIds`) are re-sent from the
 * loaded template rather than defaulted away, which is the difference between
 * a partial editor and one that quietly deletes.
 */
export function TemplatesPage() {
  const paths = useAppPaths()
  const { teams, isLoading: isLoadingContext } = useWorkspaceContext()

  const { templates, isLoading, errorMessage, retry } = useTemplateList()
  const actions = useTemplateActions()

  const [isComposerOpen, setIsComposerOpen] = useState(false)
  const [editing, setEditing] = useState<IssueTemplate | null>(null)
  const [filing, setFiling] = useState<IssueTemplate | null>(null)
  const [fileTeamId, setFileTeamId] = useState('')
  const [fileTitle, setFileTitle] = useState('')

  const [formErrors, setFormErrors] = useState<readonly TemplateValidationError[]>(NO_ERRORS)
  const [formMessage, setFormMessage] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [created, setCreated] = useState<CreatedIssue | null>(null)

  /** Workspace-wide first, then one group per team that has templates. */
  const groups = useMemo<TemplateGroup[]>(() => {
    const shared = templates.filter((template) => template.teamId === null)
    const result: TemplateGroup[] = []

    if (shared.length > 0) {
      result.push({ key: 'shared', title: 'Any team', templates: shared })
    }

    for (const team of teams) {
      const owned = templates.filter((template) => template.teamId === team.id)

      if (owned.length > 0) {
        result.push({
          key: team.id,
          title: `${team.key} · ${team.name}`,
          templates: owned,
        })
      }
    }

    /*
      Templates whose team is not in the workspace context -- a team created
      since the context was cached, say. Grouped rather than dropped: a
      template that vanished from this screen because its team was unfamiliar
      would look like data loss.
    */
    const grouped = new Set(result.flatMap((group) => group.templates.map((t) => t.id)))
    const rest = templates.filter((template) => !grouped.has(template.id))

    if (rest.length > 0) {
      result.push({ key: 'other', title: 'Other teams', templates: rest })
    }

    return result
  }, [teams, templates])

  const closeForms = useCallback(() => {
    setIsComposerOpen(false)
    setEditing(null)
    setFormErrors(NO_ERRORS)
    setFormMessage(null)
  }, [])

  const applyOutcome = useCallback(
    (outcome: Awaited<ReturnType<typeof actions.createTemplate>>) => {
      if (outcome.status === 'ok') {
        closeForms()
        return
      }

      if (outcome.status === 'rejected') {
        setFormErrors(outcome.errors)
        setFormMessage(null)
        return
      }

      setFormErrors(NO_ERRORS)
      setFormMessage(outcome.message)
    },
    [closeForms],
  )

  const handleCreate = useCallback(
    (draft: IssueTemplateDraft) => {
      void actions.createTemplate(draft).then(applyOutcome)
    },
    [actions, applyOutcome],
  )

  const handleUpdate = useCallback(
    (draft: IssueTemplateDraft) => {
      if (editing === null) {
        return
      }

      void actions.updateTemplate(editing.id, draft).then(applyOutcome)
    },
    [actions, applyOutcome, editing],
  )

  const handleDelete = useCallback(
    (template: IssueTemplate) => {
      void actions.deleteTemplate(template.id).then((outcome) => {
        if (outcome.status === 'ok') {
          setActionError(null)
          return
        }

        setActionError(
          outcome.status === 'failed'
            ? outcome.message
            : (outcome.errors[0]?.message ?? 'That template could not be deleted.'),
        )
      })
    },
    [actions],
  )

  const openFiling = useCallback((template: IssueTemplate) => {
    setCreated(null)
    setActionError(null)
    // Preselected where the template names a team; empty where it does not,
    // because there is nothing to infer one from.
    setFileTeamId(template.teamId ?? '')
    setFileTitle(template.title ?? '')
    setFiling(template)
  }, [])

  const confirmFiling = useCallback(() => {
    if (filing === null || fileTeamId === '') {
      return
    }

    const template = filing

    void actions
      .createIssue(
        template.id,
        fileTeamId,
        // An untouched box is not an override. Sending the template's own
        // title back would work, but null is what "use the template's" means.
        fileTitle.trim() === '' || fileTitle === (template.title ?? '')
          ? null
          : fileTitle.trim(),
      )
      .then((outcome) => {
        setFiling(null)

        if (outcome.status === 'ok') {
          setCreated(outcome.value)
          setActionError(null)
          return
        }

        setActionError(
          outcome.status === 'failed'
            ? outcome.message
            : (outcome.errors[0]?.message ?? 'That issue could not be created.'),
        )
      })
  }, [actions, fileTeamId, fileTitle, filing])

  const isBusy = isLoading || isLoadingContext

  return (
    <>
      <PageHeader
        title="Templates"
        description="Pre-filled issues, so recurring work is filed the same way each time."
        actions={
          <Button
            onClick={() => {
              setFormErrors(NO_ERRORS)
              setFormMessage(null)
              setIsComposerOpen(true)
            }}
            variant="primary"
          >
            New template
          </Button>
        }
      />

      <PageContent>
        {created !== null && (
          <p className={styles.actionSuccess} role="status">
            Created{' '}
            <Link className={styles.successLink} to={paths.issue(created.id)}>
              {created.identifier} · {created.title}
            </Link>
            .
          </p>
        )}

        {actionError !== null && (
          <p className={styles.actionError} role="alert">
            {actionError}
          </p>
        )}

        {isBusy && (
          <div className={styles.skeletonStack} role="status" aria-busy="true">
            <VisuallyHidden as="div">Loading templates</VisuallyHidden>
            {Array.from({ length: 4 }, (_unused, index) => (
              <Skeleton key={index} width="100%" height="2.5rem" />
            ))}
          </div>
        )}

        {!isBusy && errorMessage !== null && (
          <ErrorState
            title="Could not load templates"
            description={errorMessage}
            onRetry={retry}
          />
        )}

        {!isBusy && errorMessage === null && templates.length === 0 && (
          <EmptyState
            icon={<DocumentIcon />}
            title="No templates yet"
            description="A template holds a title, a description, a priority and an estimate, so work that recurs is filed the same way every time."
            actions={
              <Button
                onClick={() => {
                  setIsComposerOpen(true)
                }}
                variant="primary"
              >
                Create the first template
              </Button>
            }
          />
        )}

        {!isBusy &&
          errorMessage === null &&
          groups.map((group) => (
            <section
              aria-labelledby={`templates-${group.key}`}
              className={styles.group}
              key={group.key}
            >
              <div className={styles.groupHead}>
                <h2 className={styles.groupTitle} id={`templates-${group.key}`}>
                  {group.title}
                </h2>
                <span className={styles.groupCount}>{group.templates.length}</span>
              </div>

              <List label={`Templates for ${group.title}`}>
                {group.templates.map((template) => {
                  const priority =
                    template.priority === null ? null : describePriority(template.priority)

                  return (
                    <ListRow key={template.id}>
                      <ListRowMain>
                        <span className={styles.rowMain}>
                          <span className={styles.templateName}>{template.name}</span>
                          {template.title !== null && (
                            <span className={styles.templateTitle}>{template.title}</span>
                          )}
                        </span>
                      </ListRowMain>

                      <ListRowMeta>
                        {priority?.name != null && (
                          <Badge tone="neutral">{priority.name}</Badge>
                        )}
                        {template.estimate !== null && (
                          <Badge tone="neutral">{template.estimate} pt</Badge>
                        )}

                        <span className={styles.rowActions}>
                          <Button
                            onClick={() => {
                              openFiling(template)
                            }}
                            size="sm"
                          >
                            {/*
                              Every row has one of these, and so does the
                              rail. Without the hidden half the workspace
                              would contain a dozen buttons called exactly
                              "New issue", which is what a list of names reads
                              like to anyone navigating by them. The column
                              still shows two words.
                            */}
                            New issue{' '}
                            <VisuallyHidden>from {template.name}</VisuallyHidden>
                          </Button>
                          <Menu
                            align="end"
                            items={[
                              {
                                id: 'edit',
                                label: 'Edit template...',
                                onSelect: () => {
                                  setFormErrors(NO_ERRORS)
                                  setFormMessage(null)
                                  setEditing(template)
                                },
                              },
                              {
                                id: 'delete',
                                label: 'Delete template',
                                destructive: true,
                                separatorBefore: true,
                                disabled: actions.isSaving,
                                onSelect: () => {
                                  handleDelete(template)
                                },
                              },
                            ]}
                            label={`Actions on ${template.name}`}
                            size="sm"
                          />
                        </span>
                      </ListRowMeta>
                    </ListRow>
                  )
                })}
              </List>
            </section>
          ))}
      </PageContent>

      <Dialog
        open={isComposerOpen}
        onClose={closeForms}
        title="New template"
        description="The fields an issue made from this template starts with."
      >
        <TemplateForm
          errorMessage={formMessage}
          errors={formErrors}
          isSaving={actions.isSaving}
          onCancel={closeForms}
          onSubmit={handleCreate}
          submitLabel="Create template"
          teams={teams}
          template={null}
        />
      </Dialog>

      <Dialog
        open={editing !== null}
        onClose={closeForms}
        title="Edit template"
        description={editing?.name}
      >
        {editing !== null && (
          <TemplateForm
            errorMessage={formMessage}
            errors={formErrors}
            isSaving={actions.isSaving}
            onCancel={closeForms}
            onSubmit={handleUpdate}
            submitLabel="Save changes"
            teams={teams}
            template={editing}
          />
        )}
      </Dialog>

      <Dialog
        open={filing !== null}
        onClose={() => {
          setFiling(null)
        }}
        title="New issue from template"
        description={filing?.name}
      >
        <div className={styles.form}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor="templates-file-team">
              Team
            </label>
            <Select
              id="templates-file-team"
              onChange={(event) => {
                setFileTeamId(event.target.value)
              }}
              value={fileTeamId}
            >
              <option value="">Choose a team</option>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>
                  {team.key} · {team.name}
                </option>
              ))}
            </Select>
            {/* Required by the schema even when the template names a team,
                because an issue belongs to exactly one. */}
            <p className={styles.hint}>
              An issue belongs to one team, so this is always asked.
            </p>
          </div>

          <div className={styles.field}>
            <label className={styles.label} htmlFor="templates-file-title">
              Title
            </label>
            <Input
              id="templates-file-title"
              onChange={(event) => {
                setFileTitle(event.target.value)
              }}
              value={fileTitle}
            />
            <p className={styles.hint}>
              Leave it as it is to use the template's title.
            </p>
          </div>

          <div className={styles.formActions}>
            <Button
              onClick={() => {
                setFiling(null)
              }}
            >
              Cancel
            </Button>
            <Button
              disabled={fileTeamId === '' || actions.isSaving}
              onClick={confirmFiling}
              variant="primary"
            >
              Create issue
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  )
}
