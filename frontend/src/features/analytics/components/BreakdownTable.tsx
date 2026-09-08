import { peak } from '../lib/metrics'
import styles from '../analytics.module.css'

export interface BreakdownRow {
  /** Unique within one table; used as the React key. */
  key: string
  label: string
  value: number
  /**
   * Rendered instead of the bare count -- "4 of 10", "21 points". The count is
   * still what the bar is drawn from.
   */
  detail?: string
}

export interface BreakdownTableProps {
  /** The table's accessible name. Must be distinct on the page. */
  caption: string
  /** The header over the label column: "Assignee", "Priority", "Project". */
  unitLabel: string
  rows: readonly BreakdownRow[]
  /**
   * What to say when there are no rows. An empty table and a table of zeros
   * are different facts and this is the first one.
   */
  emptyMessage: string
}

/**
 * A horizontal bar chart that IS its own text alternative.
 *
 * The chart and the table are one element rather than a picture with a table
 * hidden behind a disclosure. Two reasons, and the second is the one that
 * decided it:
 *
 *   * A `<details>` per chart is a disclosure control per chart, and four
 *     controls reading "Show as table" on one screen is four buttons with the
 *     same accessible name -- the exact defect `expectNoDuplicateButtonNames`
 *     exists to catch.
 *   * A table with a bar in each row is not a compromise. The number is
 *     readable, the comparison is visible, and there is only one thing to keep
 *     in sync when the data changes.
 *
 * The bar is `aria-hidden`: the value beside it is already in the row, and a
 * screen reader announcing both would read every figure twice.
 */
export function BreakdownTable({
  caption,
  unitLabel,
  rows,
  emptyMessage,
}: BreakdownTableProps) {
  if (rows.length === 0) {
    return <p className={styles.note}>{emptyMessage}</p>
  }

  const highest = peak(rows.map((row) => row.value))

  return (
    <table className={styles.table}>
      <caption className={styles.tableCaption}>{caption}</caption>
      <thead>
        <tr>
          <th scope="col">{unitLabel}</th>
          <th scope="col">Issues</th>
          {/* No header text: the column holds a decorative bar and a column
              header for it would be announced before every empty cell. */}
          <th scope="col">
            <span className={styles.visuallyHiddenHeader}>Relative size</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key}>
            <th scope="row" className={styles.rowLabel}>
              {row.label}
            </th>
            <td className={styles.rowValue}>{row.detail ?? row.value}</td>
            <td className={styles.rowBar}>
              <svg
                className={styles.bar}
                viewBox="0 0 100 8"
                preserveAspectRatio="none"
                aria-hidden="true"
                focusable="false"
              >
                <rect className={styles.barTrack} x={0} y={0} width={100} height={8} rx={2} />
                {row.value > 0 && (
                  <rect
                    className={styles.barValue}
                    x={0}
                    y={0}
                    width={(row.value / highest) * 100}
                    height={8}
                    rx={2}
                  />
                )}
              </svg>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
