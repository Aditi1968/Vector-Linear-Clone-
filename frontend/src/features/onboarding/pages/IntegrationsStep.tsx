import { useQuery } from '@apollo/client/react'
import { Link } from 'react-router-dom'
import type { ReactNode } from 'react'

import { Badge, Spinner, VisuallyHidden } from '../../../components'
import { OnboardingIntegrationsDocument } from '../api'
import { workspaceHome } from '../lib/progress'
import { useOnboardingContext } from './OnboardingLayout'
import styles from '../onboarding.module.css'

/**
 * The three states both providers report, spelled the same way.
 *
 * `GithubIntegrationStatus` and `SlackIntegrationStatus` are separate enums
 * in the schema with identical members, so this is the shape both narrow to
 * rather than a claim that they are the same type.
 */
type IntegrationStatus = 'UNCONFIGURED' | 'DISCONNECTED' | 'CONNECTED'

interface IntegrationCardProps {
  name: string
  /** What connecting it buys, in one sentence. */
  purpose: string
  status: IntegrationStatus
  /** What it is connected to, when it is. */
  connectedTo: string | null
  /** The backend's own start route. Never a provider URL. */
  startPath: string
  workspaceSlug: string
}

/**
 * One provider, described honestly.
 *
 * The whole point of this component is that `UNCONFIGURED` does not get a
 * Connect button. It means the *deployment* holds no credentials for the
 * provider -- no GitHub App id, no Slack client id -- and the start route
 * answers 404 by design rather than explaining which secret is missing. A
 * button there would be an invitation to click something that cannot work,
 * and the resulting 404 would read as a bug in Vector rather than as a
 * deployment that never enabled the feature. So the card says it is
 * unavailable, and says why in the only terms the client is entitled to.
 *
 * ## Why the Connect control is an `<a href>` and not a `<Link>`
 *
 * `/github/install` and `/slack/oauth/start` are backend endpoints, not
 * routes in this application. They mint a single-use `state`, set it in a
 * cookie, and 302 to the provider -- so the browser has to leave the SPA
 * entirely. A `<Link>` would ask the router for a route that does not exist
 * and render the not-found page.
 *
 * ## What is deliberately not here
 *
 * A client id, a client secret, a redirect URI, a scope list, or any
 * `github.com`/`slack.com` URL assembled in this file. The authorize URL is
 * built server-side because the CSRF `state` has to be minted somewhere the
 * callback can check it against, and a frontend-built URL cannot do that. A
 * client id in this bundle would also be a client id in every user's browser
 * and in the repository.
 */
function IntegrationCard({
  name,
  purpose,
  status,
  connectedTo,
  startPath,
  workspaceSlug,
}: IntegrationCardProps) {
  let badge: ReactNode
  let detail: string
  let action: ReactNode = null

  if (status === 'CONNECTED') {
    badge = <Badge tone="success">Connected</Badge>
    detail =
      connectedTo === null ? 'Already connected.' : `Connected to ${connectedTo}.`
  } else if (status === 'DISCONNECTED') {
    badge = <Badge>Not connected</Badge>
    detail = purpose
    action = (
      <a
        className={styles.connect}
        // The workspace is named in the query string because the endpoint
        // authorizes against it before it issues a state -- an ordinary
        // member gets nothing to carry through the consent screen.
        href={`${startPath}?workspace=${encodeURIComponent(workspaceSlug)}`}
      >
        Connect {name}
      </a>
    )
  } else {
    badge = <Badge tone="neutral">Unavailable</Badge>
    detail = `This Vector deployment has no ${name} credentials configured, so there is nothing to connect to. An administrator has to set it up on the server first.`
  }

  return (
    <li className={styles.integration}>
      <div className={styles.integrationHead}>
        <h3 className={styles.integrationName}>{name}</h3>
        {badge}
      </div>
      <p className={styles.integrationDetail}>{detail}</p>
      {action}
    </li>
  )
}

/**
 * Connect GitHub and Slack, or find out plainly that you cannot.
 *
 * Always skippable. Neither integration is part of the state machine in
 * ../lib/progress.ts, because neither can ever be "unfinished" -- a
 * workspace with no GitHub connection is a finished workspace.
 */
export function IntegrationsStep() {
  const { membership } = useOnboardingContext()
  const workspaceSlug = membership?.workspace.slug ?? ''

  const { data, loading, error } = useQuery(OnboardingIntegrationsDocument, {
    variables: { workspaceSlug },
    skip: workspaceSlug === '',
  })

  const home = workspaceHome(workspaceSlug)

  return (
    <div className={styles.step}>
      <h1 className={styles.title}>Connect your tools</h1>
      <p className={styles.lede}>
        Optional, and changeable later from workspace settings.
      </p>

      {loading && (
        <p className={styles.loading}>
          <Spinner />
          <VisuallyHidden>Checking which integrations are available</VisuallyHidden>
        </p>
      )}

      {error !== undefined && (
        // Not an `ErrorState` with a retry: nothing here is required, and a
        // prominent failure for an optional step reads as though setup has
        // gone wrong. Saying so once and leaving the Finish button in place
        // is the proportionate answer.
        <p className={styles.notice} role="alert">
          Vector could not check which integrations are available. You can set
          them up later from workspace settings.
        </p>
      )}

      {data !== undefined && (
        <ul className={styles.integrations}>
          <IntegrationCard
            connectedTo={data.githubIntegration.accountLogin}
            name="GitHub"
            purpose="Link pull requests and commits to Vector issues."
            startPath="/github/install"
            status={data.githubIntegration.status}
            workspaceSlug={workspaceSlug}
          />
          <IntegrationCard
            connectedTo={data.slackIntegration.teamName}
            name="Slack"
            purpose="Post issue updates into a Slack channel."
            startPath="/slack/oauth/start"
            status={data.slackIntegration.status}
            workspaceSlug={workspaceSlug}
          />
        </ul>
      )}

      <div className={styles.actions}>
        {/*
         * The end of setup. A `<Link>` and not a `<Navigate>`: this is a
         * decision the person makes, and the workspace home is a real route
         * in this application, unlike the two connect links above.
         */}
        <Link className={styles.advance} to={home}>
          Finish and open {membership?.workspace.name ?? 'Vector'}
        </Link>
      </div>
    </div>
  )
}
