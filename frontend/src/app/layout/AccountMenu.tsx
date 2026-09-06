import { useNavigate } from 'react-router-dom'

import { Avatar, Menu, SegmentedControl, SettingsIcon, cx } from '../../components'
import type { MenuItem } from '../../components'
import { useLogout } from '../../features/auth'
import { useAppPaths } from '../routes/useAppPaths'
import { useWorkspace } from '../workspace/context'
import { useTheme } from './preferences'
import type { ThemePreference } from './preferences'
import styles from './Sidebar.module.css'

const THEME_OPTIONS: readonly { value: ThemePreference; label: string }[] = [
  { value: 'system', label: 'Auto' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
]

/**
 * The foot of the rail: who you are, how the product looks, and the way out.
 *
 * The name and email come from `me` and from nowhere else -- no initials
 * derived from a slug, no "Signed in" placeholder. A user whose profile has no
 * name is shown their email, which is the only other thing the server knows
 * about them and is what they typed to get here.
 *
 * ## Why the theme control is a radio group and not a menu item
 *
 * Three exclusive choices with one selected is exactly a radio group, and
 * `SegmentedControl` is one: arrow keys move and select, the group is a single
 * tab stop, and each option is announced with its state and position. The same
 * three as menu items would be announced as three plain commands, with the
 * current one distinguishable only by a tick that is `aria-hidden`. The
 * selected value is the whole point of this control, so it goes where the
 * platform announces it.
 *
 * It is hidden with every other label when the rail is collapsed, on the same
 * rule as the rest of the rail: collapsed is icon-only, and a labelled control
 * has nowhere to go. Expanding is one keystroke away and the choice persists.
 */
export function AccountMenu() {
  const { viewer } = useWorkspace()
  const paths = useAppPaths()
  const navigate = useNavigate()
  const logout = useLogout()
  const [theme, setTheme] = useTheme()

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
      <SegmentedControl
        label="Theme"
        options={THEME_OPTIONS}
        value={theme}
        onChange={setTheme}
        className={cx(styles.theme, styles.collapsible)}
      />

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
