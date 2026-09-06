import { Link } from 'react-router-dom'

import { ErrorState, Skeleton, VectorMark, VisuallyHidden } from '../../components'
import { paths } from '../routes/paths'
import type { WorkspaceMembership } from '../workspace/context'
import styles from './AppLayout.module.css'

/**
 * The three things the shell can be other than a workspace.
 *
 * They are laid out here rather than inside `AppLayout` so that the layout
 * file reads as the state machine it is: loading, failed, not yours, yours.
 */

/**
 * The frame while `myWorkspaces` is in flight.
 *
 * Skeletons in the shape of the rail and the page, not a spinner over the
 * whole application. The difference matters more here than on a list: this
 * query gates *every* authenticated screen, so a full-page spinner is what
 * the user sees on every cold load of the product, and a spinner communicates
 * "wait" where a skeleton communicates "here is what is coming".
 *
 * The bars are `aria-hidden` (see `Skeleton`), so the announcement is made
 * once by a live region instead of as a stack of blank list items.
 */
export function ShellSkeleton() {
  return (
    <div className={styles.shell}>
      <div className={styles.skeletonRail} aria-hidden="true">
        <Skeleton height="1.75rem" />
        <Skeleton height="2rem" />
        <Skeleton height="1.5rem" width="70%" />
        <Skeleton height="1.5rem" width="85%" />
        <Skeleton height="1.5rem" width="60%" />
      </div>

      <main className={styles.main}>
        <div className={styles.skeletonPage} aria-hidden="true">
          <Skeleton height="1.5rem" width="12rem" />
          <Skeleton height="1rem" />
          <Skeleton height="1rem" width="90%" />
          <Skeleton height="1rem" width="80%" />
        </div>

        <div role="status">
          <VisuallyHidden>Loading your workspaces</VisuallyHidden>
        </div>
      </main>
    </div>
  )
}

/**
 * The shell query failed.
 *
 * Standalone rather than inside the rail, because without the answer there is
 * no workspace to draw a rail for. No message from the failure is shown: the
 * user can do nothing with a transport error, and this component has no way
 * to tell a public backend message from an unexpected one.
 */
export function ShellError({ onRetry }: { onRetry: () => void }) {
  return (
    <main className={styles.standalone}>
      <ErrorState
        title="Could not load your workspaces"
        description="Vector could not reach the server. Check your connection and try again."
        onRetry={onRetry}
      />
    </main>
  )
}

export interface WorkspaceNotFoundProps {
  /** The slug from the URL, shown back so a mistyped address is recognisable. */
  slug: string
  /** The viewer's own workspaces, offered as a way out. */
  memberships: readonly WorkspaceMembership[]
}

/**
 * The address names a workspace this viewer cannot see.
 *
 * One screen for two situations that must stay indistinguishable: there is no
 * such workspace, and there is one but the viewer is not a member. The shell
 * cannot tell them apart even in principle -- it never asked the server about
 * this slug, it only failed to find it among the caller's own memberships --
 * and that is the strongest form of the guarantee. The server enforces the
 * same equivalence on every scoped field.
 *
 * So the copy says nothing about existence, and the rail is not rendered: a
 * half-drawn shell around a workspace the viewer has no membership in is both
 * a confusing screen and a claim the client has no standing to make.
 *
 * The way out is the viewer's own list, which is theirs to see.
 */
export function WorkspaceNotFound({ slug, memberships }: WorkspaceNotFoundProps) {
  return (
    <main className={styles.standalone}>
      <div className={styles.notFound}>
        <span className={styles.notFoundMark}>
          <VectorMark />
        </span>

        <h1 className={styles.notFoundTitle}>Workspace not found</h1>

        <p className={styles.notFoundBody}>
          Nothing at <code className={styles.notFoundSlug}>/{slug}</code> is
          open to you. The address may be mistyped, or you may need an
          invitation.
        </p>

        {memberships.length > 0 && (
          <nav aria-label="Your workspaces" className={styles.notFoundList}>
            <p className={styles.notFoundListLabel}>Your workspaces</p>
            <ul role="list" className={styles.notFoundLinks}>
              {memberships.map((membership) => (
                <li key={membership.workspace.id}>
                  <Link to={paths.workspace(membership.workspace.slug)}>
                    {membership.workspace.name}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        )}
      </div>
    </main>
  )
}
