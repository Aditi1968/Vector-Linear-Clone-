/**
 * The shared component layer.
 *
 * Deliberately four exports. This is not a component library and should not
 * become one speculatively: a primitive earns a place here when a second
 * caller needs it, not when someone anticipates one. Anything used by exactly
 * one feature belongs inside that feature.
 */

export { Button } from './Button'
export type { ButtonProps, ButtonSize, ButtonVariant } from './Button'

export { VisuallyHidden } from './VisuallyHidden'
export type { VisuallyHiddenProps } from './VisuallyHidden'

export { IssuesIcon, PlusIcon, SearchIcon, VectorMark } from './icons'
export type { IconProps } from './icons'

export { cx } from './cx'
export type { ClassValue } from './cx'
