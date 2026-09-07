import { SegmentedControl, cx } from '../../components'
import type { SegmentedOption } from '../../components'
import type { Density, GroupMode } from './preferences'
import { useDensity, useGroupMode } from './preferences'
import styles from './ViewControls.module.css'

const DENSITY_OPTIONS: readonly SegmentedOption<Density>[] = [
  { value: 'dense', label: 'Dense' },
  { value: 'comfortable', label: 'Comfortable' },
]

const GROUP_OPTIONS: readonly SegmentedOption<GroupMode>[] = [
  { value: 'flat', label: 'Flat' },
  { value: 'grouped', label: 'Grouped' },
]

export interface ViewControlsProps {
  /**
   * Whether to offer the grouping toggle.
   *
   * Off by default, because most screens have nothing to group by. A screen
   * that passes this must also read `useGroupMode()` and honour it -- a
   * control that changes a stored preference and no pixels is worse than no
   * control.
   */
  grouping?: boolean
  className?: string
}

/**
 * The two view options that live in a page header's trailing edge.
 *
 * Rendered into `PageHeader`'s `actions` slot by the screens that want them,
 * rather than by `PageHeader` itself. The header already has a slot for
 * page-scoped controls, and a screen that reaches for these is making the
 * same kind of decision as a screen that puts a filter button there. A
 * dedicated prop would only be `actions` with a narrower type.
 *
 * ## Why the state does not come through props
 *
 * Both preferences are global and both are stored (see ./preferences.ts), so
 * every instance of this control reads and writes the same two values. That
 * is what makes the choice survive a navigation: the control unmounts with
 * the old screen, the new one mounts a fresh instance, and the stored value
 * is what both of them start from.
 *
 * Density is the interesting half. Choosing it writes one attribute onto
 * `<html>` and every row in the product changes height through
 * `--row-height`; nothing subscribes and nothing re-renders. So this control
 * is genuinely fire-and-forget for the screen hosting it, and a screen that
 * renders it gets working density without a line of its own.
 *
 * Grouping is not, because it changes what is rendered rather than how tall
 * it is. A screen that passes `grouping` has to read the same hook.
 */
export function ViewControls({ grouping = false, className }: ViewControlsProps) {
  const [density, setDensity] = useDensity()
  const [groupMode, setGroupMode] = useGroupMode()

  return (
    <div className={cx(styles.controls, className)}>
      <SegmentedControl
        label="Row density"
        options={DENSITY_OPTIONS}
        value={density}
        onChange={setDensity}
      />

      {grouping && (
        <SegmentedControl
          label="Group issues"
          options={GROUP_OPTIONS}
          value={groupMode}
          onChange={setGroupMode}
        />
      )}
    </div>
  )
}
