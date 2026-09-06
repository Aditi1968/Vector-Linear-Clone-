import type { ReactNode } from 'react'

import { Button } from './Button'
import type { ButtonProps } from './Button'

export type IconButtonProps = Omit<ButtonProps, 'children' | 'icon' | 'fullWidth'> & {
  icon: ReactNode
  /** Required: there is no visible text to name this control. */
  'aria-label': string
}

/**
 * A square button that is only a glyph.
 *
 * Thin on purpose -- it is `Button` with `children` forbidden, so it inherits
 * every behaviour and every style rather than growing a parallel set that
 * drifts. What it adds is worth the file: the call site reads as what it is,
 * and the default variant is `ghost`, which is what a toolbar or row-hover
 * control almost always wants. A solid-filled icon button in a dense list is
 * usually someone reaching for `Button` and forgetting to change the variant.
 */
export function IconButton({ variant = 'ghost', ...rest }: IconButtonProps) {
  return <Button variant={variant} {...rest} />
}
