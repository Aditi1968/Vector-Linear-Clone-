import { useNavigate } from 'react-router-dom'

import {
  CheckIcon,
  ChevronDownIcon,
  Menu,
  VectorMark,
  cx,
} from '../../components'
import type { MenuItem } from '../../components'
import { paths } from '../routes/paths'
import { useWorkspace } from '../workspace/context'
import styles from './Sidebar.module.css'

/**
 * The workspace's identity at the head of the rail, and a switcher when there
 * is something to switch to.
 *
 * The switcher is present only when the viewer genuinely belongs to more than
 * one workspace, and its list is `myWorkspaces` -- the same response the shell
 * used to resolve the slug in the URL. There is no hardcoded name, no
 * hardcoded id and no "Personal workspace" placeholder anywhere in this file;
 * a tenant identifier invented by the frontend is worse than a missing
 * feature, because it looks implemented and enforces nothing.
 *
 * With exactly one workspace it renders a `<div>` rather than a disabled
 * button. A button is an affordance, and an affordance that opens a menu of
 * one item is a promise the product cannot keep.
 *
 * Destinations are built with `paths` and not `useAppPaths()`: every entry but
 * one points at a *different* workspace, and the bound helper only knows the
 * current one.
 *
 * Nothing here reads the collapsed state. The rail's CSS clips the name and
 * the chevron when it is collapsed, which keeps the trigger's accessible name
 * intact -- a JSX branch that dropped the text would leave a screen-reader
 * user with a button announced as nothing at all.
 */
export function WorkspaceSwitcher() {
  const { workspace, memberships } = useWorkspace()
  const navigate = useNavigate()

  if (memberships.length < 2) {
    return (
      <div className={styles.identity}>
        <span className={styles.mark} aria-hidden="true">
          <VectorMark />
        </span>
        <span className={cx(styles.identityName, styles.collapsible)}>
          {workspace.name}
        </span>
      </div>
    )
  }

  const items: MenuItem[] = memberships.map((membership) => ({
    id: membership.workspace.id,
    label: membership.workspace.name,
    // The current one is marked rather than omitted: a list that silently
    // drops an entry makes the reader wonder which one they are in.
    icon: membership.workspace.id === workspace.id ? <CheckIcon /> : undefined,
    onSelect: () => {
      void navigate(paths.workspace(membership.workspace.slug))
    },
  }))

  return (
    <Menu
      label="Switch workspace"
      items={items}
      variant="ghost"
      size="sm"
      align="start"
      className={styles.switcher}
    >
      <span className={styles.mark} aria-hidden="true">
        <VectorMark />
      </span>
      <span className={cx(styles.identityName, styles.collapsible)}>
        {workspace.name}
      </span>
      <ChevronDownIcon className={styles.collapsible} />
    </Menu>
  )
}
