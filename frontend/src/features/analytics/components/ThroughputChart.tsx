import type { ThroughputDay } from '../api'
import { busiestDay, peak } from '../lib/metrics'
import styles from '../analytics.module.css'

/*
 * Drawn by hand in SVG rather than with a charting library.
 *
 * Three series over at most 180 points is a `<rect>` per point and a `<path>`
 * per line; a library for that is 40kB and a second set of colour and
 * accessibility conventions to reconcile with `tokens.css`. Every colour and
 * measurement below is a `var(--*)` applied in the stylesheet, so the chart
 * follows the theme without knowing what a theme is.
 *
 * ## The viewBox does the arithmetic
 *
 * The SVG is laid out in its own coordinate space -- 0..VIEW_WIDTH by
 * 0..VIEW_HEIGHT -- and stretched to whatever width the card has by
 * `preserveAspectRatio="none"`. So nothing here measures a DOM node, nothing
 * recomputes on resize, and the component is a pure function of its data.
 *
 * The cost of that is that stroke widths stretch too, which is why the lines
 * are `vector-effect="non-scaling-stroke"`: a 1-unit stroke in a space
 * squashed 8:1 would be visibly thicker vertically than horizontally.
 */

const VIEW_WIDTH = 720
const VIEW_HEIGHT = 180

/** Room under the plot for the two date labels, in view units. */
const AXIS_HEIGHT = 18
const PLOT_HEIGHT = VIEW_HEIGHT - AXIS_HEIGHT

export interface ThroughputChartProps {
  series: readonly ThroughputDay[]
}

/**
 * Issues filed, delivered and abandoned, one point per day.
 *
 * ## Bars for what stopped, a line for what arrived
 *
 * Completed and canceled are stacked bars, because they are two parts of one
 * quantity -- the work that stopped that day -- and stacking is what makes the
 * total legible. Created is a line over the top, because it is a *different*
 * quantity and stacking it onto the others would draw a total that means
 * nothing. Whether a team is filing faster than it finishes is the comparison
 * this chart exists to support, and it is a line against a stack.
 *
 * ## The text alternative
 *
 * `role="img"` with an `aria-label` carrying the shape of the series, plus the
 * summary paragraph the page renders beneath it. A 180-row table is not an
 * alternative to this chart; it is 180 rows nobody reads. The summary names
 * the totals and the busiest day, which is what a sighted reader takes from
 * the picture.
 */
export function ThroughputChart({ series }: ThroughputChartProps) {
  const highest = peak(series.map((day) => Math.max(day.created, day.completed + day.canceled)))
  const step = VIEW_WIDTH / Math.max(1, series.length)

  // A hair of padding between bars, and never so much that a 180-day window
  // draws bars of zero width.
  const barWidth = Math.max(step * 0.55, 1)

  const scale = (value: number) => PLOT_HEIGHT - (value / highest) * PLOT_HEIGHT

  const createdLine = series
    .map((day, index) => `${(index * step + step / 2).toString()},${scale(day.created).toString()}`)
    .join(' ')

  const busiest = busiestDay(series)
  const label =
    busiest === null
      ? 'No days in this window.'
      : `Daily throughput over ${series.length.toString()} days. The busiest day was ${busiest.day}, with ${busiest.completed.toString()} issues delivered.`

  return (
    <svg
      className={styles.chart}
      viewBox={`0 0 ${VIEW_WIDTH.toString()} ${VIEW_HEIGHT.toString()}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
    >
      {/* The baseline. Drawn rather than implied, so a window of all zeros
          renders a visible axis instead of an empty box that reads as a
          rendering failure. */}
      <line
        className={styles.chartAxis}
        x1={0}
        y1={PLOT_HEIGHT}
        x2={VIEW_WIDTH}
        y2={PLOT_HEIGHT}
        vectorEffect="non-scaling-stroke"
      />

      {series.map((day, index) => {
        const x = index * step + (step - barWidth) / 2
        const completedTop = scale(day.completed)
        const canceledTop = scale(day.completed + day.canceled)

        return (
          <g key={day.day}>
            {day.completed > 0 && (
              <rect
                className={styles.chartCompleted}
                x={x}
                y={completedTop}
                width={barWidth}
                height={PLOT_HEIGHT - completedTop}
              />
            )}
            {day.canceled > 0 && (
              <rect
                className={styles.chartCanceled}
                x={x}
                y={canceledTop}
                width={barWidth}
                height={completedTop - canceledTop}
              />
            )}
          </g>
        )
      })}

      {series.length > 1 && (
        <polyline
          className={styles.chartCreated}
          points={createdLine}
          fill="none"
          vectorEffect="non-scaling-stroke"
        />
      )}
    </svg>
  )
}
