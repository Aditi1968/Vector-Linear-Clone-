import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'

import { PriorityIndicator } from './PriorityIndicator'
import { ProgressIndicator } from './ProgressIndicator'
import { RelationIndicator, relationKindFrom } from './RelationIndicator'
import { StatusIndicator, statusCategoryFrom } from './StatusIndicator'
import type { PriorityLevel } from './PriorityIndicator'
import type { StatusCategory } from './StatusIndicator'

/**
 * These four are pictures that carry meaning, so the failure mode is silence:
 * a glyph with no accessible name renders perfectly and tells a screen-reader
 * user nothing. Nothing about the drawing itself is asserted -- that is a
 * design decision and would make every tweak a test edit -- only that each one
 * announces what it means.
 */

const STATUSES: readonly StatusCategory[] = [
  'backlog',
  'unstarted',
  'started',
  'completed',
  'canceled',
]

const PRIORITIES: readonly PriorityLevel[] = [
  'none',
  'low',
  'medium',
  'high',
  'urgent',
]

describe('StatusIndicator', () => {
  it.each(STATUSES)('names the %s category', (category) => {
    render(<StatusIndicator category={category} />)

    const glyph = screen.getByRole('img')
    expect(glyph.getAttribute('aria-label')).toMatch(/^Status: \S/)
  })

  it("prefers the workflow state's own name over the generic one", () => {
    render(<StatusIndicator category="started" name="In review" />)

    expect(screen.getByRole('img')).toHaveAccessibleName('Status: In review')
  })

  it('announces once when the label is also visible', () => {
    render(<StatusIndicator category="completed" showLabel />)

    // role="img" makes the subtree presentational, so the visible text is not
    // read a second time after the label.
    expect(screen.getByRole('img')).toHaveAccessibleName('Status: Done')
  })

  it('rejects a category this build does not know instead of guessing one', () => {
    expect(statusCategoryFrom('STARTED')).toBe('started')
    expect(statusCategoryFrom('TRIAGE')).toBeNull()
  })
})

describe('PriorityIndicator', () => {
  it.each(PRIORITIES)('names the %s level', (level) => {
    render(<PriorityIndicator level={level} />)

    expect(screen.getByRole('img').getAttribute('aria-label')).toMatch(
      /^Priority: \S/,
    )
  })

  it('lets the caller name it, so the raw value can be included', () => {
    render(<PriorityIndicator level="urgent" name="1, labelled Urgent by this app" />)

    expect(screen.getByRole('img')).toHaveAccessibleName(
      'Priority: 1, labelled Urgent by this app',
    )
  })
})

describe('ProgressIndicator', () => {
  it('reports a value, a range and a spoken form', () => {
    render(<ProgressIndicator value={8} total={13} label="Cycle 12" />)

    const bar = screen.getByRole('progressbar', { name: 'Cycle 12' })
    expect(bar).toHaveAttribute('aria-valuenow', '8')
    expect(bar).toHaveAttribute('aria-valuemax', '13')
    // "62 percent" is not the number anyone is tracking.
    expect(bar).toHaveAttribute('aria-valuetext', '8 of 13')
  })

  it('survives a total of zero rather than reporting NaN', () => {
    render(<ProgressIndicator value={0} total={0} label="Empty cycle" />)

    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '0')
  })

  it('clamps a value outside the range', () => {
    render(<ProgressIndicator value={99} total={4} label="Overfull" />)

    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '4')
  })
})

describe('RelationIndicator', () => {
  it('names the relation and what is on the other end', () => {
    render(<RelationIndicator kind="blockedBy" target="VEC-214" />)

    expect(screen.getByRole('img')).toHaveAccessibleName('Blocked by VEC-214')
  })

  it('distinguishes blocking from being blocked', () => {
    render(
      <>
        <RelationIndicator kind="blocks" />
        <RelationIndicator kind="blockedBy" />
      </>,
    )

    const [blocks, blockedBy] = screen.getAllByRole('img')
    expect(blocks).toHaveAccessibleName('Blocks')
    expect(blockedBy).toHaveAccessibleName('Blocked by')
  })

  it('maps the wire enum, and refuses values it does not know', () => {
    expect(relationKindFrom('BLOCKED_BY')).toBe('blockedBy')
    expect(relationKindFrom('DUPLICATE')).toBe('duplicate')
    expect(relationKindFrom('SUPERSEDES')).toBeNull()
  })
})
