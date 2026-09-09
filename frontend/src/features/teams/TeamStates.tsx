import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../app/layout'
import { useAppPaths } from '../../app/routes'
import { ErrorState, Skeleton, VisuallyHidden } from '../../components'
import shared from '../screens.module.css'
import type { TeamResolution } from './api'

export interface TeamStateScreenProps {
  /** Any resolution that is not `found`. `found` renders the real screen. */
  resolution: Exclude<TeamResolution, { status: 'found' }>
}

/**
 * The three things a team screen can be instead of a team.
 *
 * Shared by both team screens because the answer to "which team is this" is
 * the same question on both, and two copies would eventually give two
 * different sentences for the same missing key.
 *
 * The distinction that matters is between `failed` and `missing`. Both leave
 * the screen with no team; one is a request that did not arrive and offers a
 * retry, the other is a URL that names nothing and offers a way back. A
 * product that renders "team not found" for a network failure sends the user
 * looking for a team that is there.
 *
 * Note that `missing` is also what a workspace the viewer cannot see produces
 * -- the server answers NOT_FOUND identically for a workspace that does not
 * exist and one the caller may not see, and the shell has already refused to
 * render a screen for a slug that is not among the viewer's memberships. So
 * this is never a partial screen: there is no team, and it says so.
 */
export function TeamStateScreen({ resolution }: TeamStateScreenProps) {
  const paths = useAppPaths()

  if (resolution.status === 'loading') {
    return (
      <>
        <PageHeader title="Team" />
        <PageContent>
          <div className={shared.skeletonStack} role="status" aria-busy="true">
            {/* `Skeleton` is `aria-hidden` by design, so the announcement
                belongs on the live region around it. */}
            <VisuallyHidden as="div">Loading team</VisuallyHidden>
            <Skeleton width="18rem" height="2rem" />
            <Skeleton width="100%" height="8rem" />
          </div>
        </PageContent>
      </>
    )
  }

  if (resolution.status === 'failed') {
    return (
      <>
        <PageHeader title="Team" />
        <PageContent constrained>
          <ErrorState
            title="Could not load this workspace's teams"
            description={resolution.message}
            onRetry={resolution.retry}
          />
        </PageContent>
      </>
    )
  }

  return (
    <>
      <PageHeader title="Team not found" />
      <PageContent constrained>
        <div className={shared.stack}>
          <p>
            No team in this workspace has the key{' '}
            <span className={shared.identifier}>{resolution.key}</span>. It may
            have been renamed, or the address may have been mistyped.
          </p>
          <p>
            <Link to={paths.issues()}>Back to issues</Link>
          </p>
        </div>
      </PageContent>
    </>
  )
}
