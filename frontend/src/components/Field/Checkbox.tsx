import type { ComponentPropsWithRef } from 'react'

import { CheckIcon } from '../icons'
import { cx } from '../cx'
import styles from './Field.module.css'

export type CheckboxProps = Omit<ComponentPropsWithRef<'input'>, 'type'>

/**
 * A checkbox.
 *
 * The native input is kept and repainted with `appearance: none`; the tick is
 * a sibling SVG revealed by `:checked`. Nothing about the control's behaviour
 * is reimplemented -- space toggles it, a `<label for>` still targets it, and
 * it reports itself to assistive technology as a checkbox with a state.
 *
 * The indeterminate ("some selected") state is not modelled. There is no
 * attribute for it -- it is a DOM property that must be assigned through a
 * ref -- and nothing in the product needs a tri-state box yet. Whoever builds
 * bulk selection should add it here rather than beside their list.
 */
export function Checkbox({ className, ...rest }: CheckboxProps) {
  return (
    <span className={cx(styles.checkboxRoot, className)}>
      <input type="checkbox" className={styles.checkboxInput} {...rest} />
      <CheckIcon className={styles.checkboxMark} />
    </span>
  )
}
