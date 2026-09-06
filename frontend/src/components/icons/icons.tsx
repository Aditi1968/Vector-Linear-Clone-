import type { SVGProps } from 'react'

/**
 * Vector's icon set.
 *
 * Original geometry, drawn here rather than pulled from an icon package. That
 * is partly a dependency decision -- an icon library is a lot of bytes and a
 * lot of licence surface for six glyphs -- and partly a correctness one: every
 * icon in this file obeys the same three rules, which a third-party set would
 * not.
 *
 *   1. `aria-hidden` is baked in and cannot be overridden away. Icons in this
 *      product are always decorative; the accessible name comes from adjacent
 *      text or from the control's `aria-label`. An icon that announced itself
 *      would double up every nav item ("list Issues").
 *   2. Sized in `em`, so an icon tracks the font size of whatever it sits in
 *      and never needs a `size` prop threaded through three components.
 *   3. Stroked in `currentColor`, so an icon inherits hover, active and
 *      disabled colour from its parent for free.
 *
 * The 16-unit viewBox is chosen to match the UI's density: these are drawn to
 * read at 14-16px, not scaled down from 24.
 */

/**
 * Props an icon accepts. `aria-hidden` is excluded from the type rather than
 * merely defaulted -- rule 1 above is not a default, it is an invariant, and
 * a caller that needs a *labelled* graphic wants an `<img>` or a titled
 * `<svg>`, not one of these.
 */
export type IconProps = Omit<SVGProps<SVGSVGElement>, 'aria-hidden' | 'children'>

const BASE_PROPS = {
  width: '1em',
  height: '1em',
  viewBox: '0 0 16 16',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.5,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
  // Keeps IE/legacy-Edge-style focusability off the element; harmless
  // elsewhere and cheap insurance against an SVG entering the tab order.
  focusable: false,
} as const

/** Three stacked rules: the issue list. */
export function IssuesIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M2.75 4.25h10.5M2.75 8h10.5M2.75 11.75h7" />
    </svg>
  )
}

/** Magnifier. */
export function SearchIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <circle cx="7.25" cy="7.25" r="4.25" />
      <path d="m10.5 10.5 2.75 2.75" />
    </svg>
  )
}

/** Plus: the create affordance. */
export function PlusIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M8 3.25v9.5M3.25 8h9.5" />
    </svg>
  )
}

/**
 * Vector's brand mark.
 *
 * Literally a vector: a marked origin with a directed segment leaving it. The
 * origin dot is what distinguishes the glyph from a generic "arrow up-right"
 * -- a vector is a point plus a direction, and drawing both is the whole
 * idea.
 *
 * Vector's own mark. No third-party logo, wordmark or trademark appears
 * anywhere in this product's branding.
 */
export function VectorMark(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <circle cx="3.9" cy="12.1" r="1.15" fill="currentColor" stroke="none" />
      <path d="M5.1 10.9 11.9 4.1" />
      <path d="M6.9 4.1h5v5" />
    </svg>
  )
}
