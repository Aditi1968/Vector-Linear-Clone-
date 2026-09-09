import { Badge, Button } from '../../../components'
import type { IndexState } from '../api'
import { describeCoverage } from '../lib/indexState'
import styles from '../semanticSearch.module.css'

export interface IndexPanelProps {
  state: IndexState | null
  isLoading: boolean
  errorMessage: string | null
  /** How many embeddings the last sweep wrote. Null until one has run. */
  written: number | null
  refreshErrorMessage: string | null
  isRefreshing: boolean
  onRefresh: () => void
  onRetry: () => void
}

/**
 * What this search is currently capable of finding.
 *
 * ## Why the numbers are on the screen rather than behind a disclosure
 *
 * They are not diagnostics. `indexed`, `pending` and `failed` are the terms in
 * which every answer this screen gives has to be read: ten matches out of a
 * half-built index are not the ten best matches, and no result at all out of
 * an empty one is not a statement about the workspace. Hiding them would make
 * the search look more certain than it is, which is the exact defect
 * `embeddingIndexingState` was added to the schema to prevent.
 *
 * ## `enabled: false` is not a count of zero
 *
 * When the deployment has no embedder the server returns all three counts as
 * zero WITHOUT LOOKING, so rendering "0 indexed" beside "0 waiting" would be
 * indistinguishable from a workspace with no issues. That case gets its own
 * sentence and no numbers.
 */
export function IndexPanel({
  state,
  isLoading,
  errorMessage,
  written,
  refreshErrorMessage,
  isRefreshing,
  onRefresh,
  onRetry,
}: IndexPanelProps) {
  return (
    <section aria-labelledby="semantic-index-title" className={styles.indexPanel}>
      <div className={styles.indexHead}>
        <h2 className={styles.indexTitle} id="semantic-index-title">
          Index
        </h2>
        {/* Neutral and not `info` for the enabled case. `info` is the cyan
            tone, and cyan in this product marks where you are and what is
            live -- it is never a badge. Spending it on a steady-state fact
            would put a second claim on the one signal that only works while
            it makes one. The warning tone stays: "Not available" is the half
            a reader has to notice, and amber is doing a different job. Neither
            badge rests on its colour anyway; the words differ entirely. */}
        {state !== null && (
          <Badge tone={state.enabled ? 'neutral' : 'warning'}>
            {state.enabled ? 'Enabled' : 'Not available'}
          </Badge>
        )}
      </div>

      {isLoading && state === null && (
        <p className={styles.hint} role="status">
          Reading the index…
        </p>
      )}

      {errorMessage !== null && (
        <div className={styles.indexError}>
          <p className={styles.hint} role="alert">
            The index could not be read, so nothing on this screen can say how complete
            an answer is: {errorMessage}
          </p>
          <Button onClick={onRetry} size="sm" variant="secondary">
            Retry reading the index
          </Button>
        </div>
      )}

      {/* Counts only when there is an index to count. See the note above on
          why `enabled: false` does not render three zeroes. */}
      {state !== null && state.enabled && (
        <dl className={styles.counts}>
          <div className={styles.countCell}>
            <dt>Indexed</dt>
            <dd className={styles.countValue}>{state.indexed}</dd>
          </div>
          <div className={styles.countCell}>
            <dt>Waiting</dt>
            <dd className={styles.countValue}>{state.pending}</dd>
          </div>
          <div className={styles.countCell}>
            <dt>Failed</dt>
            <dd className={styles.countValue}>{state.failed}</dd>
          </div>
        </dl>
      )}

      <p className={styles.hint}>{describeCoverage(state)}</p>

      {state !== null && state.enabled && (
        <>
          <div className={styles.indexActions}>
            <Button disabled={isRefreshing} onClick={onRefresh} variant="secondary">
              {isRefreshing ? 'Indexing…' : 'Index a batch now'}
            </Button>
            {written !== null && (
              <span className={styles.hint} role="status">
                {written === 0
                  ? 'That sweep wrote nothing — there was nothing waiting.'
                  : `That sweep indexed ${written} ${written === 1 ? 'issue' : 'issues'}.`}
              </span>
            )}
          </div>

          {refreshErrorMessage !== null && (
            <p className={styles.hint} role="alert">
              {refreshErrorMessage}
            </p>
          )}

          <p className={styles.hint}>
            One batch at a time, bounded by the server. A backlog larger than a batch
            needs this pressing again — or a deployment running the background worker,
            which drains it without anybody watching.
          </p>
        </>
      )}
    </section>
  )
}
