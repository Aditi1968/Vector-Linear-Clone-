import type { ReactElement } from 'react'

import { cx } from '../cx'
import styles from './indicators.module.css'

/**
 * How one issue relates to another.
 *
 * The first four mirror the server's `IssueRelationType`. `subIssue` and
 * `parent` are not relation *rows* -- they are the parent/child edge, which
 * the schema models as a field -- but they render in the same places and read
 * as the same category of fact to a user, so they share the primitive.
 */
export type RelationKind =
  | 'blocks'
  | 'blockedBy'
  | 'related'
  | 'duplicate'
  | 'subIssue'
  | 'parent'

const KIND_NAMES: Record<RelationKind, string> = {
  blocks: 'Blocks',
  blockedBy: 'Blocked by',
  related: 'Related',
  duplicate: 'Duplicate',
  subIssue: 'Sub-issue',
  parent: 'Parent issue',
}

const WIRE_KINDS: Record<string, RelationKind> = {
  BLOCKS: 'blocks',
  BLOCKED_BY: 'blockedBy',
  RELATED: 'related',
  DUPLICATE: 'duplicate',
}

/**
 * Narrow a GraphQL `IssueRelationType` to a kind, or `null` for a value this
 * build does not know. Parent/child edges do not come through here; they are
 * not relation types on the wire.
 */
export function relationKindFrom(value: string): RelationKind | null {
  return WIRE_KINDS[value] ?? null
}

/**
 * Blocking is the one relation that changes what you can *do*, so the two
 * blocking glyphs are the two that must not be told apart by colour: a circle
 * with a diagonal slash for "blocked by", an octagon with a horizontal bar for
 * "blocks". Different outline, different bar angle -- two independent cues
 * before the amber/red tint is considered at all.
 */
const GLYPHS: Record<RelationKind, ReactElement> = {
  blockedBy: (
    <>
      <circle cx="8" cy="8" r="5.3" />
      <path d="M4.25 11.75 11.75 4.25" />
    </>
  ),
  blocks: (
    <>
      <path d="M5.6 2.6h4.8l3.0 3.0v4.8l-3.0 3.0H5.6l-3.0-3.0V5.6Z" />
      <path d="M5.5 8h5" />
    </>
  ),
  related: (
    <>
      <circle cx="4.3" cy="4.3" r="1.9" />
      <circle cx="11.7" cy="11.7" r="1.9" />
      <path d="M5.85 5.85 10.15 10.15" />
    </>
  ),
  duplicate: (
    <>
      <rect x="2.6" y="2.6" width="8" height="8" rx="2.3" />
      <path d="M13.4 5.6v5.5a2.3 2.3 0 0 1-2.3 2.3H5.6" />
    </>
  ),
  subIssue: (
    <>
      <path d="M4.4 2.75v5.4a2.1 2.1 0 0 0 2.1 2.1h2.3" />
      <rect x="9.2" y="8.35" width="4.05" height="4.05" rx="1.3" />
    </>
  ),
  /* Same geometry mirrored: the branch now climbs to the parent. */
  parent: (
    <>
      <path d="M4.4 2.75v5.4a2.1 2.1 0 0 0 2.1 2.1h2.3" />
      <rect x="9.2" y="8.35" width="4.05" height="4.05" rx="1.3" />
    </>
  ),
}

export interface RelationIndicatorProps {
  kind: RelationKind
  /**
   * What is on the other end -- "VEC-214", or "3 issues". Appended to the
   * relation name so the announcement is "Blocked by VEC-214" rather than a
   * bare "Blocked by" with no object.
   */
  target?: string
  showLabel?: boolean
  className?: string
}

/** See `StatusIndicator` for why the name lives on the wrapper as `role="img"`. */
export function RelationIndicator({
  kind,
  target,
  showLabel = false,
  className,
}: RelationIndicatorProps) {
  const name = KIND_NAMES[kind]
  const accessibleName = target === undefined ? name : `${name} ${target}`

  return (
    <span
      role="img"
      aria-label={accessibleName}
      data-kind={kind}
      className={cx(styles.indicator, styles.relation, className)}
    >
      <svg
        viewBox="0 0 16 16"
        className={cx(styles.glyph, kind === 'parent' && styles.flipY)}
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        {GLYPHS[kind]}
      </svg>
      {showLabel && <span className={styles.label}>{accessibleName}</span>}
    </span>
  )
}
