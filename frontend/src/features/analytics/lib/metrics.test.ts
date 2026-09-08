import { describe, expect, it } from 'vitest'

import {
  denseStateMix,
  densePriorityMix,
  describeCycleTimeCoverage,
  describeEstimateTotal,
  describeSample,
  estimateUnit,
  formatHours,
  formatRate,
  hasMixedScales,
  peak,
} from './metrics'

/**
 * The derivations, without a render.
 *
 * Every one of these functions exists because the obvious rendering would be
 * a lie of a specific kind, so every test below names the lie it prevents
 * rather than restating the implementation.
 */

describe('formatting a duration', () => {
  it('reads hours below a day and days above one', () => {
    expect(formatHours(6)).toBe('6h')
    expect(formatHours(36)).toBe('1.5d')
  })

  it('rounds rather than truncating', () => {
    // 23.97h truncated reads 23.9h, which is wrong in the flattering
    // direction -- and this whole screen is written against numbers that
    // flatter.
    expect(formatHours(23.97)).toBe('24h')
  })

  it('shows a dash rather than a number it cannot compute', () => {
    expect(formatHours(Number.NaN)).toBe('—')
    expect(formatHours(-1)).toBe('—')
  })
})

describe('formatting a completion rate', () => {
  it('renders a rate as a whole percentage', () => {
    expect(formatRate(0.75)).toBe('75%')
  })

  it('renders an absent rate as a dash and never as zero', () => {
    /*
      THE LIE THIS PREVENTS. The server sends null when nothing stopped in the
      window. "0%" for a fortnight in which nothing was abandoned either
      reports a total failure to deliver that did not happen.
    */
    expect(formatRate(null)).toBe('—')
    expect(formatRate(0)).toBe('0%')
  })
})

describe('describing a sample', () => {
  it('warns when a percentile is over too few issues to mean anything', () => {
    expect(describeSample({ __typename: 'DurationSummary', count: 3, medianHours: 4, p90Hours: 9 })).toMatch(
      /too few/i,
    )
  })

  it('says nothing finished rather than implying a fast window', () => {
    expect(describeSample(null)).toMatch(/nothing finished/i)
  })

  it('states the sample plainly once it is large enough', () => {
    expect(
      describeSample({ __typename: 'DurationSummary', count: 40, medianHours: 4, p90Hours: 9 }),
    ).toBe('Over 40 issues.')
  })
})

describe('describing what cycle time covers', () => {
  const summary = (measured: number, completedTotal: number) => ({
    __typename: 'CycleTimeSummary' as const,
    measured,
    completedTotal,
    medianHours: 12,
    p90Hours: 30,
  })

  it('refuses the figure when nothing could be measured', () => {
    /*
      THE LIE THIS PREVENTS, and the worst one on the screen. With no
      measurable issue the server still sends a summary -- carrying zeros --
      so that "0 of 96" can be reported. Rendering the median from it would
      print "0h", which reads as instant delivery: the most flattering
      possible misreading of an absent measurement.
    */
    const coverage = describeCycleTimeCoverage(summary(0, 96))

    expect(coverage.isMeasurable).toBe(false)
    expect(coverage.note).toContain('96')
    expect(coverage.note).toMatch(/lead time/i)
  })

  it('names both numbers when the coverage is partial', () => {
    const coverage = describeCycleTimeCoverage(summary(41, 96))

    expect(coverage.isMeasurable).toBe(true)
    expect(coverage.note).toContain('41')
    expect(coverage.note).toContain('96')
  })

  it('still says what it covered when it covered everything', () => {
    // A caveat that appears only when coverage is partial teaches the reader
    // that an uncaveated figure is complete -- so the complete case says so.
    expect(describeCycleTimeCoverage(summary(96, 96)).note).toMatch(/all 96/)
  })

  it('distinguishes "nothing was delivered" from "nothing could be measured"', () => {
    // The two call for opposite conclusions and would be one sentence if the
    // absent summary and the zeroed one were collapsed.
    expect(describeCycleTimeCoverage(null).note).toMatch(/nothing was delivered/i)
  })
})

describe('estimate units', () => {
  it('has no unit for t-shirt sizes', () => {
    /*
      The mixed-scale rule in one return value. A t-shirt estimate is a rung
      on a ladder, so a sum of two is not a quantity in any unit and there is
      no word to print after it.
    */
    expect(estimateUnit('TSHIRT')).toBeNull()
    expect(estimateUnit('POINTS')).toBe('points')
    expect(estimateUnit('HOURS')).toBe('hours')
  })

  it('tells a sized t-shirt team apart from an unsized one', () => {
    // Collapsing these would tell a team that estimates diligently in t-shirt
    // sizes that it estimates nothing.
    expect(describeEstimateTotal(null, 7, 'TSHIRT')).toMatch(/7 sized/)
    expect(describeEstimateTotal(null, 0, 'TSHIRT')).toBe('Not sized')
  })

  it('prints the unit beside a real total', () => {
    expect(describeEstimateTotal(21, 5, 'POINTS')).toBe('21 points')
  })

  it('notices when two teams disagree about what an estimate means', () => {
    expect(hasMixedScales([{ estimateScale: 'POINTS' }, { estimateScale: 'HOURS' }])).toBe(true)
    expect(hasMixedScales([{ estimateScale: 'POINTS' }, { estimateScale: 'POINTS' }])).toBe(false)
  })
})

describe('filling an axis', () => {
  it('gives every status a bar, including the empty ones', () => {
    /*
      The server omits categories holding nothing, because filling them there
      would put the axis in eleven statements instead of one place. A chart
      missing a bar reads as a chart with fewer categories.
    */
    const rows = denseStateMix([{ __typename: 'StateCategoryCount', category: 'STARTED', issues: 3 }])

    expect(rows).toHaveLength(5)
    expect(rows.map((row) => row.issues)).toEqual([0, 0, 3, 0, 0])
  })

  it('treats priority zero as a level and not as an absence', () => {
    // 0 is "no priority" rather than the lowest, and it is usually the
    // largest bucket -- dropping it would hide most of the backlog.
    const rows = densePriorityMix([{ __typename: 'PriorityCount', priority: 0, issues: 12 }])

    expect(rows[0]?.label).toBe('No priority')
    expect(rows[0]?.issues).toBe(12)
  })
})

describe('scaling a chart', () => {
  it('never returns a zero to divide by', () => {
    // Every bar in a workspace with no issues is zero, and a chart that
    // divided by the peak would render NaN across the whole axis.
    expect(peak([0, 0, 0])).toBe(1)
    expect(peak([])).toBe(1)
  })
})
