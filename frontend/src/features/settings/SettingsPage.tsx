import { useState } from 'react'
import type { ReactNode } from 'react'

import { PageContent, PageHeader } from '../../app/layout'
import { paths } from '../../app/routes/paths'
import { useWorkspace } from '../../app/workspace'
import {
  Button,
  Checkbox,
  Dialog,
  ErrorState,
  Select,
  Spinner,
  Tag,
  VisuallyHidden,
  cx,
} from '../../components'
import { integrationStartHref } from '../../lib/integrations'
import shared from '../screens.module.css'
import styles from './Settings.module.css'
import { useIntegrations } from './api'
import type {
  GithubAutomation,
  GithubIntegration,
  IntegrationStatus,
  SettingsTeam,
} from './api'

/**
 * The backend's own OAuth entry points.
 *
 * Paths on this origin, not provider URLs. `/github/install` and
 * `/slack/oauth/start` are FastAPI routes (`app/rest/github.py`,
 * `app/rest/slack.py`): each mints a single-use CSRF `state`, sets it in a
 * cookie and 302s to the provider. Assembling a `github.com/login/oauth`
 * URL here instead would need a client id in this bundle -- which means in
 * every user's browser and in the repository -- and could not mint a state
 * the callback is able to check.
 *
 * Not in `src/app/routes/paths.ts`, deliberately: nothing here is a route in
 * this application, and putting a backend endpoint in the path helper is how
 * one ends up being rendered by the router as a not-found page.
 */
const START_PATHS = {
  github: '/github/install',
  slack: '/slack/oauth/start',
} as const

interface IntegrationPanelProps {
  name: string
  /**
   * The provider's initials, engraved on the plate at the head of the panel.
   *
   * A prop rather than `name.slice(0, 2)`, which reads "GI" for GitHub. Two
   * call sites is cheaper than a mapping table nobody would think to update.
   */
  glyph: string
  /** What connecting it buys, in one sentence. */
  purpose: string
  status: IntegrationStatus
  /** What it is connected to, when it is. */
  connectedTo: string | null
  /** The backend's own start route. Never a provider URL. */
  startPath: string
  workspaceSlug: string
  /**
   * Where the provider's callback should leave the browser, as a path on this
   * origin.
   *
   * Required rather than defaulted, because the default is what broke: with
   * no `return_to` the backend falls back to the first allowed ORIGIN, which
   * has no path, and a completed connection lands on the home page. See
   * `lib/integrations`.
   */
  returnToPath: string
  /** Facts worth showing about a live connection: repositories, scopes. */
  details?: ReactNode
  isDisconnecting: boolean
  onDisconnect: () => void
}

/**
 * One provider, described honestly.
 *
 * The point of this component is that `UNCONFIGURED` does not get a Connect
 * button. It means the deployment holds no credentials for the provider, and
 * the start route answers 404 by design rather than explaining which secret
 * is missing. A button there is an invitation to click something that cannot
 * work, and the resulting 404 reads as a bug in Vector rather than as a
 * deployment that never enabled the feature. So the panel says the
 * integration is unavailable, and says why in the only terms the client is
 * entitled to.
 *
 * ## Why `PENDING` is a branch of its own
 *
 * GitHub gained a fourth status when the server stopped believing the
 * installation id its own callback was handed. It means the workspace has
 * claimed an installation and GitHub has not confirmed it -- so there is
 * nothing to show about an account or a repository, and there may never be.
 * Rendering it as CONNECTED is the defect this screen would be re-committing;
 * rendering it as DISCONNECTED would tell a user to click Connect at a claim
 * that is already theirs. It gets its own badge, its own sentence, and a way
 * out in both directions: run the install again, or cancel the claim.
 *
 * `features/onboarding` reaches the same conclusion for its setup step. This
 * is a second component rather than a shared one because it has a control
 * that step does not -- disconnecting -- and reaching across a feature
 * boundary for an un-exported card would couple two screens that only look
 * alike.
 *
 * ## Why Connect is an `<a href>` and not a `<Link>`
 *
 * The browser has to leave the SPA entirely: these are backend endpoints, not
 * routes in this application. A `<Link>` would ask the router for a route
 * that does not exist and render the not-found page.
 *
 * ## Why the state is a dot and a word, and Connect is silver
 *
 * The connection state was a `Badge`, and for `PENDING` a cyan one. Cyan in
 * this product marks where you are and what is live; spending it on a status
 * pill is a third claim on a signal that only works while it has one. So the
 * state is now a mono readout with a dot taking `currentColor` -- the word
 * carries the meaning and the dot is redundant -- and cyan is left to the
 * rail.
 *
 * Connect is the primary action on this panel, so it wears the silver
 * gradient and its lit top edge rather than the accent. Disconnect is a
 * hairline box: it opens a confirmation, and putting the destructive paint on
 * the thing that *asks* rather than on the thing that *does* spends the
 * warning a step early. The dialog's own Disconnect is still `danger`.
 */
function IntegrationPanel({
  name,
  glyph,
  purpose,
  status,
  connectedTo,
  startPath,
  workspaceSlug,
  returnToPath,
  details,
  isDisconnecting,
  onDisconnect,
}: IntegrationPanelProps) {
  const [confirming, setConfirming] = useState(false)
  const headingId = `integration-${name.toLowerCase()}`

  // The workspace is named in the query string because the endpoint
  // authorizes against it before it issues a state -- an ordinary member gets
  // nothing to carry through the consent screen. `return_to` is named for a
  // different reason: without it a completed connection ends on the home page
  // rather than back here. See lib/integrations.
  const startHref = integrationStartHref(startPath, workspaceSlug, returnToPath)

  const state =
    status === 'CONNECTED'
      ? { word: 'Connected', tone: styles.stateOk }
      : status === 'PENDING'
        ? { word: `Waiting for ${name}`, tone: styles.statePending }
        : status === 'DISCONNECTED'
          ? { word: 'Not connected', tone: styles.stateOff }
          : { word: 'Unavailable', tone: styles.stateOff }

  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <div className={styles.panelHead}>
        {/* The name printed beside it, so announcing it would say the
            provider twice. */}
        <span className={styles.glyph} aria-hidden="true">
          {glyph}
        </span>

        <div className={styles.identity}>
          <h2 className={styles.panelTitle} id={headingId}>
            {name}
          </h2>
          <p className={cx(styles.state, state.tone)}>
            <span className={styles.dot} aria-hidden="true" />
            {state.word}
          </p>
        </div>

        {status === 'DISCONNECTED' && (
          <a className={styles.connect} href={startHref}>
            Connect {name}
          </a>
        )}

        {(status === 'CONNECTED' || status === 'PENDING') && (
          /* One control for both, because cancelling an unconfirmed claim is
             the same mutation as disconnecting a live one -- it is
             idempotent about which state it is ending -- and only the wording
             differs. */
          <Button
            loading={isDisconnecting}
            onClick={() => {
              setConfirming(true)
            }}
          >
            {status === 'CONNECTED' ? `Disconnect ${name}` : `Cancel ${name} claim`}
          </Button>
        )}
      </div>

      <div className={styles.panelBody}>
        {status === 'CONNECTED' && (
          <>
            <p className={styles.panelNote}>
              {connectedTo === null
                ? `${name} is connected to this workspace.`
                : `Connected to ${connectedTo}.`}
            </p>
            {details}
          </>
        )}

        {status === 'PENDING' && (
          <>
            <p className={styles.panelNote}>
              This workspace has claimed a {name} installation and {name} has
              not confirmed it yet. Nothing is connected until it does, and
              nothing about the account or its repositories is shown until
              then. This usually takes a moment. If it has not cleared after a
              few minutes, remove the app on {name} and install it again --
              that is what makes {name} send the confirmation afresh.
            </p>
            <p>
              <a href={startHref}>Run the {name} installation again</a>
            </p>
          </>
        )}

        {status === 'DISCONNECTED' && (
          <p className={styles.panelNote}>{purpose}</p>
        )}

        {status === 'UNCONFIGURED' && (
          <p className={styles.panelNote}>
            This Vector deployment has no {name} credentials configured, so
            there is nothing to connect to. An administrator has to set it up
            on the server first; nothing on this screen can.
          </p>
        )}
      </div>

      <Dialog
        open={confirming}
        onClose={() => {
          setConfirming(false)
        }}
        title={`Disconnect ${name}?`}
        description={`This workspace will stop receiving ${name} activity. You can connect it again afterwards.`}
        footer={
          <div className={shared.rowActions}>
            <Button
              onClick={() => {
                setConfirming(false)
              }}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={isDisconnecting}
              onClick={() => {
                setConfirming(false)
                onDisconnect()
              }}
            >
              Disconnect
            </Button>
          </div>
        }
      >
        <p>
          Existing links between Vector issues and {name} are not deleted, but
          no new ones will be created.
        </p>
      </Dialog>
    </section>
  )
}

interface GithubDetailsProps {
  github: GithubIntegration
  teams: SettingsTeam[]
  isSaving: boolean
  onTrackedChange: (repositoryIds: string[]) => void
  onAutomationChange: (input: {
    teamId: string
    enabled: boolean
    startedStateId?: string | null
    completedStateId?: string | null
  }) => void
}

/**
 * What a connected GitHub installation is doing here: which repositories this
 * workspace takes activity from, and what a pull request does to its issues.
 *
 * ## Why the repositories are checkboxes and not tags
 *
 * An installation covers what the GitHub admin granted, which for an
 * organisation is routinely everything. A workspace wants development activity
 * from the handful it works in. Unticking one stops new deliveries being
 * applied; it deletes nothing, and the history already collected stays on the
 * issues showing it.
 *
 * Each tick submits the WHOLE set, because that is what the mutation takes --
 * sending only the change would let two admins on two tabs interleave into a
 * selection neither chose.
 *
 * ## Why the automation is a checkbox and then two selects
 *
 * Workflow states belong to a TEAM and are named by it, so there is no global
 * "In Progress" to default to and a team may have several states of the same
 * category. The checkbox asks the server for the sensible default -- the first
 * `started` and first `completed` state on that team's board -- which it stores
 * explicitly and hands straight back, so the selects below it immediately show
 * two named states rather than a rule. Changing either is an ordinary write.
 *
 * A team with no row is a team with no automation, which is every team until
 * somebody ticks the box. That is the opt-in, and it is why the checkbox is
 * unchecked by default rather than reflecting some ambient setting.
 */
function GithubDetails({
  github,
  teams,
  isSaving,
  onTrackedChange,
  onAutomationChange,
}: GithubDetailsProps) {
  const tracked = github.repositories.filter((one) => one.tracked)
  const automations = new Map<string, GithubAutomation>(
    github.automations.map((automation) => [automation.teamId, automation]),
  )

  return (
    <>
      <fieldset className={styles.group}>
        <legend className={styles.groupLabel}>Repositories</legend>

        {github.repositories.length === 0 ? (
          <p className={shared.footnote}>
            No repositories are visible to this installation yet.
          </p>
        ) : (
          <>
            <p className={shared.footnote}>
              Vector applies pull requests and pushes only from the repositories
              ticked here. Unticking one keeps the activity already collected.
            </p>
            {github.repositories.map((repository) => (
              <label className={styles.settingRow} key={repository.repositoryId}>
                <Checkbox
                  checked={repository.tracked}
                  disabled={isSaving}
                  onChange={(event) => {
                    const next = event.currentTarget.checked
                      ? [...tracked.map((one) => one.repositoryId), repository.repositoryId]
                      : tracked
                          .filter((one) => one.repositoryId !== repository.repositoryId)
                          .map((one) => one.repositoryId)

                    onTrackedChange(next)
                  }}
                />
                <span className={styles.settingLabel}>{repository.fullName}</span>
              </label>
            ))}
          </>
        )}
      </fieldset>

      {teams.map((team) => {
        const automation = automations.get(team.id)
        const started = team.workflowStates.filter(
          (state) => state.category === 'STARTED',
        )
        const completed = team.workflowStates.filter(
          (state) => state.category === 'COMPLETED',
        )

        return (
          <fieldset className={styles.group} key={team.id}>
            <legend className={styles.groupLabel}>
              Pull request automation — {team.name}
            </legend>

            <label className={styles.settingRow}>
              <Checkbox
                checked={automation !== undefined}
                disabled={isSaving}
                onChange={(event) => {
                  onAutomationChange({
                    teamId: team.id,
                    enabled: event.currentTarget.checked,
                  })
                }}
              />
              <span className={styles.settingLabel}>
                Move {team.key} issues when a pull request that names them opens
                or merges
              </span>
            </label>

            {automation !== undefined && (
              <>
                {/*
                  The team's key is in the label and not only in the legend
                  above it. A workspace with two automated teams otherwise
                  puts two controls called "Pull request opened" on one
                  screen, which is ambiguous to anyone driving this by voice
                  or by a rotor list -- neither of which reads the legend
                  first.
                */}
                <label className={styles.settingRow}>
                  <span className={styles.settingLabel}>
                    Pull request opened — {team.key}
                  </span>
                  <Select
                    value={automation.startedStateId ?? ''}
                    disabled={isSaving}
                    onChange={(event) => {
                      onAutomationChange({
                        teamId: team.id,
                        enabled: true,
                        startedStateId: event.currentTarget.value || null,
                        completedStateId: automation.completedStateId,
                      })
                    }}
                  >
                    <option value="">Do nothing</option>
                    {started.map((state) => (
                      <option key={state.id} value={state.id}>
                        {state.name}
                      </option>
                    ))}
                  </Select>
                </label>

                <label className={styles.settingRow}>
                  <span className={styles.settingLabel}>
                    Pull request merged — {team.key}
                  </span>
                  <Select
                    value={automation.completedStateId ?? ''}
                    disabled={isSaving}
                    onChange={(event) => {
                      onAutomationChange({
                        teamId: team.id,
                        enabled: true,
                        startedStateId: automation.startedStateId,
                        completedStateId: event.currentTarget.value || null,
                      })
                    }}
                  >
                    <option value="">Do nothing</option>
                    {completed.map((state) => (
                      <option key={state.id} value={state.id}>
                        {state.name}
                      </option>
                    ))}
                  </Select>
                </label>

                <p className={shared.footnote}>
                  A draft pull request moves nothing until it is marked ready
                  for review, and one closed without merging is not a
                  completion. An issue somebody has already moved on is left
                  where they put it.
                </p>
              </>
            )}
          </fieldset>
        )
      })}
    </>
  )
}

/**
 * Workspace settings: identity, and the two integrations.
 *
 * The identity block is read-only, and that is the schema rather than a
 * choice: there is no `workspaceUpdate` mutation, so a name or slug field
 * here would be a form with nowhere to submit to. It says so rather than
 * showing disabled inputs, which read as a permission problem.
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`; this renders a fragment and adds neither.
 */
export function SettingsPage() {
  const { workspace, slug, role } = useWorkspace()

  const {
    github,
    slack,
    teams,
    isLoading,
    errorMessage,
    actionErrorMessage,
    isDisconnecting,
    isSaving,
    retry,
    disconnectGithub,
    disconnectSlack,
    setTrackedRepositories,
    setAutomation,
  } = useIntegrations()

  return (
    <>
      <PageHeader
        title="Settings"
        description={`Workspace settings for ${workspace.name}.`}
      />

      <PageContent>
        <div className={shared.stack}>
          {actionErrorMessage !== null && (
            <p className={shared.formError} role="alert">
              {actionErrorMessage}
            </p>
          )}

          <section className={styles.panel} aria-labelledby="settings-identity">
            <div className={styles.panelHead}>
              <h2 className={styles.sectionLabel} id="settings-identity">
                Workspace
              </h2>
            </div>

            <div className={styles.panelBody}>
              <dl className={styles.facts}>
                <dt className={styles.factTerm}>Name</dt>
                <dd className={styles.factValue}>{workspace.name}</dd>

                <dt className={styles.factTerm}>URL</dt>
                <dd className={styles.factValue}>
                  {/* The slug from the address bar and not the one on the
                      membership: they are the same string, and the address
                      bar is what a user would copy. */}
                  <span className={shared.identifier}>/{slug}</span>
                </dd>

                <dt className={styles.factTerm}>Your role</dt>
                <dd className={styles.factValue}>
                  {/* A readout, not a pill. It was a cyan `Badge` for
                      anything above MEMBER, which is cyan doing a badge's
                      job -- and the word was always what carried it.
                      Uppercased in CSS rather than in the string, so what a
                      screen reader receives is the word "Owner" and not an
                      acronym it may decide to spell out. */}
                  <span className={styles.roleValue}>
                    {role.charAt(0) + role.slice(1).toLowerCase()}
                  </span>
                </dd>
              </dl>

              <p className={styles.panelNote}>
                The name and URL cannot be changed here: the API exposes no
                workspace update, so there is nothing this screen could submit
                to.
              </p>
            </div>
          </section>

          {isLoading && (
            <div role="status">
              <Spinner />
              <VisuallyHidden>Checking which integrations are available</VisuallyHidden>
            </div>
          )}

          {!isLoading && errorMessage !== null && github === null && (
            <ErrorState
              title="Could not load integrations"
              description={errorMessage}
              onRetry={retry}
            />
          )}

          {github !== null && (
            <IntegrationPanel
              name="GitHub"
              glyph="GH"
              purpose="Link pull requests and commits to Vector issues."
              status={github.status}
              connectedTo={github.accountLogin}
              startPath={START_PATHS.github}
              workspaceSlug={slug}
              returnToPath={paths.settings(slug)}
              isDisconnecting={isDisconnecting}
              onDisconnect={disconnectGithub}
              details={
                <GithubDetails
                  github={github}
                  teams={teams}
                  isSaving={isSaving}
                  onTrackedChange={setTrackedRepositories}
                  onAutomationChange={setAutomation}
                />
              }
            />
          )}

          {slack !== null && (
            <IntegrationPanel
              name="Slack"
              glyph="SL"
              purpose="Post issue updates into a Slack channel."
              status={slack.status}
              connectedTo={slack.teamName}
              startPath={START_PATHS.slack}
              workspaceSlug={slug}
              returnToPath={paths.settings(slug)}
              isDisconnecting={isDisconnecting}
              onDisconnect={disconnectSlack}
              details={
                slack.scopes.length > 0 ? (
                  <div className={shared.rowActions}>
                    {/* Shown because a connection granted fewer scopes than
                        Vector needs is a real state, and "Connected" alone
                        would not distinguish it. */}
                    <span className={styles.sectionLabel}>Granted scopes</span>
                    {slack.scopes.map((scope) => (
                      <Tag key={scope} name={scope} />
                    ))}
                  </div>
                ) : undefined
              }
            />
          )}
        </div>
      </PageContent>
    </>
  )
}
