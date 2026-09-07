import type { SVGProps } from 'react'

/**
 * Vector's icon set.
 *
 * Original geometry, drawn here rather than pulled from an icon package. That
 * is partly a dependency decision -- an icon library is a lot of bytes and a
 * lot of licence surface -- and partly a correctness one: every icon in this
 * file obeys the same three rules, which a third-party set would not.
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
 *
 * What is deliberately *not* here: the workflow-state and priority glyphs.
 * Those are in `components/indicators`, because they are not decoration --
 * they carry meaning, they must announce it, and their shape is chosen so the
 * meaning survives without colour. Filing them next to the chevrons would
 * invite someone to render one `aria-hidden` beside no text at all.
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

/** Solid shapes opt out of the stroked default rather than restating it. */
const SOLID = { fill: 'currentColor', stroke: 'none' } as const

/* ================================================================== */
/* Brand                                                               */
/* ================================================================== */

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
      <circle cx="3.9" cy="12.1" r="1.15" {...SOLID} />
      <path d="M5.1 10.9 11.9 4.1" />
      <path d="M6.9 4.1h5v5" />
    </svg>
  )
}

/* ================================================================== */
/* Domain objects                                                      */
/* ================================================================== */

/** Three stacked rules: the issue *list*. Used for navigation, not for one issue. */
export function IssuesIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M2.75 4.25h10.5M2.75 8h10.5M2.75 11.75h7" />
    </svg>
  )
}

/**
 * A single issue.
 *
 * The rounded square is Vector's unit-of-work silhouette and is shared with
 * the status glyphs in `components/indicators`, so an issue in a breadcrumb
 * and an issue in a list read as the same kind of thing.
 */
export function IssueIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <rect x="2.75" y="2.75" width="10.5" height="10.5" rx="2.9" />
    </svg>
  )
}

/** A project: an isometric box, i.e. a thing issues are packed into. */
export function ProjectIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M8 2.1 13.5 5.1v5.8L8 13.9 2.5 10.9V5.1Z" />
      <path d="M2.6 5.2 8 8.1l5.4-2.9M8 8.1v5.7" />
    </svg>
  )
}

/**
 * The board: the same issues, stood up in columns.
 *
 * Deliberately `IssuesIcon` rotated -- three rules turned on their side --
 * because that is exactly what the screen is. The columns are of unequal
 * height, so the glyph reads as a board with uneven columns rather than as a
 * bar chart.
 */
export function BoardIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M3.4 2.9v10.2M8 2.9v6.6M12.6 2.9v8.4" />
    </svg>
  )
}

/** A cycle: a loop that closes, with the arrowhead showing it repeats. */
export function CycleIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M13.1 8a5.1 5.1 0 1 1-1.9-3.97" />
      <path d="M13.35 2.6v2.6h-2.6" />
    </svg>
  )
}

/** A team: two people, the nearer one overlapping. */
export function TeamIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <circle cx="6.1" cy="5.6" r="2.35" />
      <path d="M1.9 13.1c0-2.1 1.88-3.5 4.2-3.5s4.2 1.4 4.2 3.5" />
      <path d="M10.6 3.6a2.35 2.35 0 0 1 0 4.5M12 9.9c1.3.45 2.1 1.6 2.1 3.2" />
    </svg>
  )
}

/** A label: a tag with its eyelet. */
export function LabelIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M2.75 7.4V3.6a.85.85 0 0 1 .85-.85h3.8c.22 0 .44.09.6.25l5 5a.85.85 0 0 1 0 1.2l-3.8 3.8a.85.85 0 0 1-1.2 0l-5-5a.85.85 0 0 1-.25-.6Z" />
      <circle cx="5.6" cy="5.6" r="1" />
    </svg>
  )
}

/** A comment: a speech bubble with the tail on the leading edge. */
export function CommentIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M13.25 9.7a1.8 1.8 0 0 1-1.8 1.8H6.6l-3.05 2.25V4.5a1.8 1.8 0 0 1 1.8-1.8h6.1a1.8 1.8 0 0 1 1.8 1.8Z" />
    </svg>
  )
}

/** A relation: two issues linked, direction unspecified. */
export function RelationIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <circle cx="4.3" cy="4.3" r="1.9" />
      <circle cx="11.7" cy="11.7" r="1.9" />
      <path d="M5.85 5.85 10.15 10.15" />
    </svg>
  )
}

/** Blocked: the prohibition sign, which needs no translating. */
export function BlockedIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <circle cx="8" cy="8" r="5.25" />
      <path d="M4.29 11.71 11.71 4.29" />
    </svg>
  )
}

/** A sub-issue: a branch dropping out of a parent into a child. */
export function SubIssueIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M4.4 2.75v5.4a2.1 2.1 0 0 0 2.1 2.1h2.3" />
      <rect x="9.2" y="8.35" width="4.05" height="4.05" rx="1.3" />
    </svg>
  )
}

/* ================================================================== */
/* Chrome                                                              */
/* ================================================================== */

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

/** Close / dismiss / remove. */
export function CloseIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="m4.1 4.1 7.8 7.8M11.9 4.1l-7.8 7.8" />
    </svg>
  )
}

/** Confirmation: selection ticks, checkbox marks, "done". */
export function CheckIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="m3.4 8.3 3.1 3.1 6.1-6.8" />
    </svg>
  )
}

/** Partial selection: the indeterminate checkbox. */
export function MinusIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M3.75 8h8.5" />
    </svg>
  )
}

export function ChevronRightIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="m6.25 3.5 4.5 4.5-4.5 4.5" />
    </svg>
  )
}

export function ChevronLeftIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M9.75 3.5 5.25 8l4.5 4.5" />
    </svg>
  )
}

export function ChevronDownIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="m3.5 6.25 4.5 4.5 4.5-4.5" />
    </svg>
  )
}

export function ChevronUpIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="m3.5 9.75 4.5-4.5 4.5 4.5" />
    </svg>
  )
}

/** Overflow: "there are more actions than fit here". */
export function MoreIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <circle cx="3.5" cy="8" r="1.2" {...SOLID} />
      <circle cx="8" cy="8" r="1.2" {...SOLID} />
      <circle cx="12.5" cy="8" r="1.2" {...SOLID} />
    </svg>
  )
}

/**
 * A document: a page with ruled lines, corner folded.
 *
 * The fold is what separates it from the generic rectangle a card or a panel
 * would use, and it survives 14px because it is one straight cut rather than
 * a curl.
 */
export function DocumentIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M9.1 2.2H5.2a1.6 1.6 0 0 0-1.6 1.6v8.4a1.6 1.6 0 0 0 1.6 1.6h5.6a1.6 1.6 0 0 0 1.6-1.6V5.5Z" />
      <path d="M9.1 2.2v3.3h3.3M6 8.6h4M6 11h2.6" />
    </svg>
  )
}

/**
 * A favourite: an outlined star.
 *
 * Outlined rather than solid because the rail draws the *set* of favourites,
 * not the act of favouriting -- a filled star reads as "this one is starred"
 * and would be wrong on a section heading. The per-row toggle fills it.
 */
export function StarIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M8 2.4l1.72 3.49 3.85.56-2.79 2.71.66 3.84L8 11.19l-3.44 1.81.66-3.84L2.43 6.45l3.85-.56Z" />
    </svg>
  )
}

/**
 * Settings: sliders, not a gear.
 *
 * A gear's teeth turn to mush below about 20px, and this icon is rendered at
 * 14. Two labelled tracks stay readable at any size and say the same thing.
 */
export function SettingsIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M2.5 5.25h2.9M8.15 5.25h5.35M2.5 10.75h5.35M10.6 10.75h2.9" />
      <circle cx="6.75" cy="5.25" r="1.75" />
      <circle cx="9.25" cy="10.75" r="1.75" />
    </svg>
  )
}

/** Inbox: a tray with the mouth cut into it. */
export function InboxIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M2.75 9.1 4.6 3.7a1.15 1.15 0 0 1 1.09-.78h4.62a1.15 1.15 0 0 1 1.09.78l1.85 5.4v2.55a1.65 1.65 0 0 1-1.65 1.65H4.4a1.65 1.65 0 0 1-1.65-1.65Z" />
      <path d="M2.75 9.1h2.9l.8 1.6h3.1l.8-1.6h2.9" />
    </svg>
  )
}

/** Something went wrong, and it is not the user's doing. */
export function AlertIcon(props: IconProps) {
  return (
    <svg {...BASE_PROPS} {...props}>
      <path d="M8 2.6a1.2 1.2 0 0 1 1.04.6l4.35 7.55a1.2 1.2 0 0 1-1.04 1.8H3.65a1.2 1.2 0 0 1-1.04-1.8L6.96 3.2A1.2 1.2 0 0 1 8 2.6Z" />
      <path d="M8 6.5v2.4" />
      <circle cx="8" cy="10.85" r="0.75" {...SOLID} />
    </svg>
  )
}
