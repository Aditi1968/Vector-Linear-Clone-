import { useId } from 'react'
import type { ReactNode } from 'react'

import { cx } from '../cx'
import styles from './SegmentedControl.module.css'

export interface SegmentedOption<Value extends string> {
  value: Value
  label: string
  /** Decorative leading glyph. */
  icon?: ReactNode
}

export interface SegmentedControlProps<Value extends string> {
  /** Names the group -- "Group issues by". */
  label: string
  options: readonly SegmentedOption<Value>[]
  value: Value
  onChange: (value: Value) => void
  className?: string
}

/**
 * One choice out of a few, shown as a row of segments.
 *
 * These are radio buttons wearing a different coat, and that is the entire
 * design. The browser already implements everything a segmented control needs:
 * arrow keys move between options *and* select as they go, the group is a
 * single tab stop, only one can be checked, and the whole thing is announced
 * as a radio group with a position ("2 of 4"). A version built from `<button>`
 * and `aria-pressed` has to reimplement all of that and typically stops after
 * the click handler.
 *
 * Grouping comes from the shared `name`, generated per instance so two
 * controls on one page cannot capture each other's options.
 */
export function SegmentedControl<Value extends string>({
  label,
  options,
  value,
  onChange,
  className,
}: SegmentedControlProps<Value>) {
  const name = useId()

  return (
    <div role="radiogroup" aria-label={label} className={cx(styles.root, className)}>
      {options.map((option) => (
        <label key={option.value} className={styles.option}>
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={option.value === value}
            onChange={() => {
              onChange(option.value)
            }}
            className={styles.input}
          />
          {option.icon !== undefined && (
            <span className={styles.icon}>{option.icon}</span>
          )}
          {option.label}
        </label>
      ))}
    </div>
  )
}
