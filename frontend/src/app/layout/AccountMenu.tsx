import { useNavigate } from 'react-router-dom'

import { Avatar, Menu, SettingsIcon, cx } from '../../components'
import type { MenuItem } from '../../components'
import { useLogout } from '../../features/auth'
import { useAppPaths } from '../routes/useAppPaths'
import { useWorkspace } from '../workspace/context'
import styles from './Sidebar.module.css'

/**
 * The foot of the rail: who you are, and the way out.
 *
 * The name and email come from `me` and from nowhere else -- no initials
 * derived from a slug, no "Signed in" placeholder. A user whose profile has no
 * name is shown their email, which is the only other thing the server knows
 * about them and is what they typed to get here.
 *
 * ## There is no theme control here, deliberately
 *
 * There was one -- an Auto/Light/Dark radio group -- and it did nothing. Not
 * "did nothing on some platforms": nothing read what it wrote. The product
 * ships one palette, `tokens.css` pins `color-scheme: dark` on bare `:root`,
 * and there is no `prefers-color-scheme` block or `:root[data-theme='light']`
 * rule anywhere in the stylesheets. So all three options rendered the same
 * navy, and the only observable effect of choosing one was that the segment
 * moved.
 *
 * A control that visibly does nothing is worse than an absent one: it teaches
 * a user that the product's settings are decorative, and it costs a support
 * conversation every time somebody picks Light and files a bug. So it is gone
 * until there is a palette to switch to.
 *
 * `./preferences` keeps `useTheme` and its storage untouched on purpose.
 * Nothing here reads it, so it costs a few unreferenced lines; deleting it
 * would throw away a correct implementation of persistence, `matchMedia`
 * tracking and the system/explicit distinction, all of which a light theme
 * will need on the day it arrives -- and would silently discard the choice
 * already stored in the browsers of anyone who used the control before it was
 * removed. A stored `light` is simply not read; if the palette lands, it
 * starts being honoured again with no migration.
 *
 * `app/layout/accountMenu.test.tsx` fails if a theme control comes back
 * before that palette does.
 */
export function AccountMenu() {
  const { viewer } = useWorkspace()
  const paths = useAppPaths()
  const navigate = useNavigate()
  const { logout } = useLogout()

  const displayName = viewer?.name ?? viewer?.email ?? 'Your account'

  const items: MenuItem[] = [
    {
      id: 'settings',
      label: 'Workspace settings',
      icon: <SettingsIcon />,
      onSelect: () => {
        void navigate(paths.settings())
      },
    },
    {
      id: 'sign-out',
      label: 'Sign out',
      destructive: true,
      separatorBefore: true,
      onSelect: () => {
        void logout()
      },
    },
  ]

  return (
    <div className={styles.account}>
      <div className={styles.accountRow}>
        {/* Decorative: the name is beside it, and announcing initials as well
          * as the name reads the person out twice. */}
        <Avatar name={displayName} size="sm" decorative />

        <span className={cx(styles.accountText, styles.collapsible)}>
          <span className={styles.accountName}>{displayName}</span>
          {viewer !== null && viewer.name !== null && (
            <span className={styles.accountEmail}>{viewer.email}</span>
          )}
        </span>

        {/* Not collapsible. Sign out is the one control that must stay
          * reachable in every state of the rail. */}
        <Menu label="Account" items={items} align="start" className={styles.accountMenu} />
      </div>
    </div>
  )
}
