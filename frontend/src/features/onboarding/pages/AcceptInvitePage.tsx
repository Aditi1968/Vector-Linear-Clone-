import { useEffect, useRef, useState } from 'react'
import { Navigate, useParams } from 'react-router-dom'

import { Spinner, VectorMark, VisuallyHidden } from '../../../components'
import { useAcceptInvitation } from '../api'
import { INVITE_TOKEN_PARAM, workspaceHome } from '../lib/progress'
import styles from '../onboarding.module.css'

type State =
  | { phase: 'redeeming' }
  | { phase: 'joined'; slug: string }
  | { phase: 'refused'; message: string }

/**
 * The other end of the link the invite step copies -- "join a workspace".
 *
 * This ships with the step that produces the link rather than being left for
 * later, because a copy control handing out a URL that 404s is worse than no
 * copy control at all.
 *
 * ## Redeeming on mount, and why that is safe here
 *
 * Opening this URL *is* the decision to accept -- somebody sent the link, the
 * recipient clicked it -- so a confirmation screen would be a step that
 * exists only to be clicked through. It is a mutation fired from an effect,
 * which is normally a smell; what makes it correct is that redeeming is
 * idempotent in the direction that matters. A token already accepted by this
 * same person is refused with `INVALID`, not double-applied, and React 19's
 * StrictMode double-invocation is guarded by the ref below so the second
 * invocation does not turn a successful join into a spurious "no longer
 * valid".
 *
 * ## What the failure says, and what it does not
 *
 * `invitationAccept` answers one deliberately vague `INVALID` -- "This
 * invitation is no longer valid" -- for a token that is unknown, revoked,
 * expired or already used. That ambiguity is the point: distinguishing them
 * would turn this endpoint into an oracle for guessing tokens. So this page
 * shows what the server said and does not embellish it.
 *
 * An unauthenticated visitor gets a top-level `UNAUTHENTICATED` instead,
 * which arrives here as the message on the error. Sending them to a sign-in
 * screen and back is `features/auth`'s to build; until it exists, saying so
 * is better than a blank page.
 */
export function AcceptInvitePage() {
  const params = useParams()
  const token = params[INVITE_TOKEN_PARAM] ?? ''
  const { acceptInvitation } = useAcceptInvitation()

  const [state, setState] = useState<State>({ phase: 'redeeming' })

  // One redemption per mount. StrictMode invokes effects twice in
  // development, and the second call would present a freshly accepted
  // invitation as an invalid one.
  const attempted = useRef(false)

  useEffect(() => {
    if (attempted.current || token === '') {
      return
    }

    attempted.current = true

    void acceptInvitation(token).then((outcome) => {
      if (outcome.status === 'ok') {
        setState({ phase: 'joined', slug: outcome.value.slug })
        return
      }

      setState({
        phase: 'refused',
        message:
          outcome.status === 'failed'
            ? outcome.message
            : // Server-chosen wording, not ours. See the note above.
              outcome.errors.map((error) => error.message).join(' '),
      })
    })
  }, [acceptInvitation, token])

  if (state.phase === 'joined') {
    // `myWorkspaces` was refetched before this resolved, so the workspace is
    // already in the cache the destination will read.
    return <Navigate replace to={workspaceHome(state.slug)} />
  }

  return (
    <main className={styles.page}>
      <div className={styles.frame}>
        <p className={styles.brand}>
          <VectorMark />
          <span>Vector</span>
        </p>

        {state.phase === 'redeeming' ? (
          <p className={styles.loading}>
            <Spinner />
            <VisuallyHidden>Accepting your invitation</VisuallyHidden>
          </p>
        ) : (
          <div className={styles.step}>
            <h1 className={styles.title}>This invitation did not work</h1>
            <p className={styles.lede} role="alert">
              {state.message}
            </p>
            <p className={styles.hint}>
              Ask whoever invited you to send a new link.
            </p>
          </div>
        )}
      </div>
    </main>
  )
}
