import { useState } from 'react'
import type { ReactNode } from 'react'

import { PageContent, PageHeader } from '../../app/layout'
import { useWorkspace } from '../../app/workspace'
import {
  Badge,
  Button,
  Dialog,
  ErrorState,
  Spinner,
  Tag,
  VisuallyHidden,
} from '../../components'
import styles from '../screens.module.css'
import { useIntegrations } from './api'
import type { IntegrationStatus } from './api'

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
  /** What connecting it buys, in one sentence. */
  purpose: string
  status: IntegrationStatus
  /** What it is connected to, when it is. */
  connectedTo: string | null
  /** The backend's own start route. Never a provider URL. */
  startPath: string
  workspaceSlug: string
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
 */
function IntegrationPanel({
  name,
  purpose,
  status,
  connectedTo,
  startPath,
  workspaceSlug,
  details,
  isDisconnecting,
  onDisconnect,
}: IntegrationPanelProps) {
  const [confirming, setConfirming] = useState(false)
  const headingId = `integration-${name.toLowerCase()}`

  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <div className={styles.panelHeader}>
        <h2 className={styles.panelTitle} id={headingId}>
          {name}
        </h2>

        {status === 'CONNECTED' ? (
          <Badge tone="success">Connected</Badge>
        ) : status === 'PENDING' ? (
          <Badge tone="info">Waiting for {name}</Badge>
        ) : status === 'DISCONNECTED' ? (
          <Badge>Not connected</Badge>
        ) : (
          <Badge tone="warning">Unavailable</Badge>
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
            <div className={styles.rowActions}>
              <Button
                variant="danger"
                loading={isDisconnecting}
                onClick={() => {
                  setConfirming(true)
                }}
              >
                Disconnect {name}
              </Button>
            </div>
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
              <a href={`${startPath}?workspace=${encodeURIComponent(workspaceSlug)}`}>
                Run the {name} installation again
              </a>
            </p>
            <div className={styles.rowActions}>
              {/* The same Disconnect the connected state offers, because
                  cancelling a claim is the other way out and the mutation is
                  idempotent about which state it is ending. */}
              <Button
                variant="danger"
                loading={isDisconnecting}
                onClick={() => {
                  setConfirming(true)
                }}
              >
                Cancel {name} claim
              </Button>
            </div>
          </>
        )}

        {status === 'DISCONNECTED' && (
          <>
            <p className={styles.panelNote}>{purpose}</p>
            <p>
              {/*
                The workspace is named in the query string because the
                endpoint authorizes against it before it issues a state -- an
                ordinary member gets nothing to carry through the consent
                screen.
              */}
              <a href={`${startPath}?workspace=${encodeURIComponent(workspaceSlug)}`}>
                Connect {name}
              </a>
            </p>
          </>
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
          <div className={styles.rowActions}>
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
    isLoading,
    errorMessage,
    actionErrorMessage,
    isDisconnecting,
    retry,
    disconnectGithub,
    disconnectSlack,
  } = useIntegrations()

  return (
    <>
      <PageHeader
        title="Settings"
        description={`Workspace settings for ${workspace.name}.`}
      />

      <PageContent>
        <div className={styles.stack}>
          {actionErrorMessage !== null && (
            <p className={styles.formError} role="alert">
              {actionErrorMessage}
            </p>
          )}

          <section className={styles.panel} aria-labelledby="settings-identity">
            <div className={styles.panelHeader}>
              <h2 className={styles.panelTitle} id="settings-identity">
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
                  <span className={styles.identifier}>/{slug}</span>
                </dd>

                <dt className={styles.factTerm}>Your role</dt>
                <dd className={styles.factValue}>
                  <Badge tone={role === 'MEMBER' ? 'neutral' : 'info'}>
                    {role.charAt(0) + role.slice(1).toLowerCase()}
                  </Badge>
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
              purpose="Link pull requests and commits to Vector issues."
              status={github.status}
              connectedTo={github.accountLogin}
              startPath={START_PATHS.github}
              workspaceSlug={slug}
              isDisconnecting={isDisconnecting}
              onDisconnect={disconnectGithub}
              details={
                github.repositories.length > 0 ? (
                  <div className={styles.rowActions}>
                    <span className={styles.footnote}>Repositories:</span>
                    {github.repositories.map((repository) => (
                      <Tag key={repository.repositoryId} name={repository.fullName} />
                    ))}
                  </div>
                ) : (
                  <p className={styles.footnote}>
                    No repositories are visible to this installation yet.
                  </p>
                )
              }
            />
          )}

          {slack !== null && (
            <IntegrationPanel
              name="Slack"
              purpose="Post issue updates into a Slack channel."
              status={slack.status}
              connectedTo={slack.teamName}
              startPath={START_PATHS.slack}
              workspaceSlug={slug}
              isDisconnecting={isDisconnecting}
              onDisconnect={disconnectSlack}
              details={
                slack.scopes.length > 0 ? (
                  <div className={styles.rowActions}>
                    {/* Shown because a connection granted fewer scopes than
                        Vector needs is a real state, and "Connected" alone
                        would not distinguish it. */}
                    <span className={styles.footnote}>Granted scopes:</span>
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
