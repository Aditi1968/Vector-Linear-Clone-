import { VectorMark } from '../../components'
import styles from './WorkspaceBadge.module.css'

/**
 * Vector's mark and name at the head of the sidebar. Presentation only.
 *
 * This is the position a workspace switcher will one day occupy, and it is
 * emphatically not one today. Vector has no workspace GraphQL surface, no
 * workspace table exposed to the client, no authenticated user and therefore
 * no set of workspaces to switch between. A switcher here would have to be
 * driven by a hardcoded name or a hardcoded UUID, and a hardcoded tenant
 * identifier in the frontend is worse than a missing feature: it is a
 * tenancy model that looks implemented, invites the next person to trust it,
 * and enforces nothing -- while the server, which is the only place isolation
 * can be enforced, knows nothing about it.
 *
 * So it renders the product's own name, statically.
 *
 * Concretely, it is a `<div>` and not a `<button>`. That is the load-bearing
 * decision. A `<button>` here would be keyboard-focusable and announced as
 * "Vector, button", which tells a screen reader user there is something to
 * activate; there is not. Nothing in this component is interactive, has a
 * hover state, or takes focus.
 *
 * When workspaces land, this file becomes the switcher and the sidebar does
 * not change.
 */
export function WorkspaceBadge() {
  return (
    <div className={styles.workspace}>
      <span className={styles.mark}>
        <VectorMark />
      </span>
      <span className={styles.name}>Vector</span>
    </div>
  )
}
