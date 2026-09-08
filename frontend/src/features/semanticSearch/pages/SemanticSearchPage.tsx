import { useId, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../../../app/layout'
import { useAppPaths } from '../../../app/routes'
import {
  Avatar,
  Button,
  EmptyState,
  ErrorState,
  Input,
  List,
  ListRow,
  ListRowMain,
  ListRowMeta,
  PriorityIndicator,
  SearchIcon,
  Spinner,
  StatusIndicator,
  Textarea,
  VisuallyHidden,
  statusCategoryFrom,
} from '../../../components'
import { memberLabel, useWorkspaceContext } from '../../issues/api'
import { describePriority, priorityLevel } from '../../issues/lib/priority'
import {
  useEmbeddingRefresh,
  useIndexState,
  useSemanticMatches,
} from '../api'
import { IndexPanel } from '../components/IndexPanel'
import { explainEmptyAnswer, similarityPercent } from '../lib/indexState'
import styles from '../semanticSearch.module.css'

/** `app/services/search.py` TITLE_MAX_LENGTH. Enforced natively, then again server-side. */
const TITLE_MAX_LENGTH = 500

/** `app/services/search.py` DESCRIPTION_MAX_LENGTH. */
const DESCRIPTION_MAX_LENGTH = 10000

/**
 * Search this workspace's issues by what they mean.
 *
 * ## Why this screen exists beside `/search`, and what it sends
 *
 * `features/search` sends `search`, which is the right field for it: hybrid
 * when the deployment has an embedder -- the lexical index from migration 011
 * fused with the vector index from 025 -- and lexical-only otherwise. Its
 * lexical arm ALWAYS runs, so an empty answer from it genuinely means no row
 * contained the words and none was near enough in meaning.
 *
 * This screen sends `issueDuplicateSuggestions`, which is the embeddings-only
 * field. That is the difference between the two screens and it is the whole of
 * it: here you describe an issue in your own words and get back the issues
 * that mean the same thing, ranked by similarity, sharing no vocabulary
 * necessarily. "The build keeps dying" finds "CI fails intermittently", which
 * a lexical index scores at zero.
 *
 * The cost of that is an answer that can be empty for two completely different
 * reasons -- nothing is similar, or nothing is indexed -- and
 * `SearchService.suggest_duplicates` returns the same empty list for both.
 * `embeddingIndexingState` is the field that separates them, this screen sends
 * it before anybody types, and ../lib/indexState.ts is where the rule about
 * what an empty answer may claim lives.
 *
 * ## Why there is a submit button and no debounce
 *
 * `/search` debounces the URL and searches as you type, which is right for a
 * one-line query over a text index. This one embeds its input: every request
 * runs a model over up to ten and a half thousand characters, and typing a
 * paragraph would be a hundred of them. The text is also too long to live in a
 * URL, so a search here is not linkable -- which is a real difference from
 * `/search` and is why the two screens are not one.
 */
export function SemanticSearchPage() {
  const paths = useAppPaths()
  const fieldId = useId()

  const [titleDraft, setTitleDraft] = useState('')
  const [descriptionDraft, setDescriptionDraft] = useState('')

  /** What was actually asked. Separate from the draft; see the note above. */
  const [asked, setAsked] = useState({ title: '', description: '' })

  const { matches, isIdle, isLoading, errorMessage, retry } = useSemanticMatches(
    asked.title,
    asked.description,
  )

  const index = useIndexState()
  const refresh = useEmbeddingRefresh()

  // For drawing a status glyph and an avatar from the UUIDs a result carries.
  // A failure costs those two columns and nothing else.
  const { stateById, memberById } = useWorkspaceContext()

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()

    setAsked({ title: titleDraft, description: descriptionDraft })
  }

  const showResults = !isIdle && !isLoading && errorMessage === null
  const empty = explainEmptyAnswer(index.state)

  return (
    <>
      <PageHeader
        title="Semantic search"
        description="Search by what an issue means rather than by the words it contains."
      />

      <PageContent>
        <div className={styles.split}>
          <div className={styles.column}>
            <form className={styles.form} noValidate onSubmit={handleSubmit}>
              <div className={styles.field}>
                {/* "Describe the issue" rather than "Title": this box is not
                    naming a thing that exists, it is the text the server
                    embeds and compares. Also keeps it from colliding with the
                    "Details" box below by more than one word. */}
                <label className={styles.label} htmlFor={`${fieldId}-title`}>
                  Describe the issue
                </label>
                <Input
                  aria-describedby={`${fieldId}-title-hint`}
                  autoComplete="off"
                  id={`${fieldId}-title`}
                  maxLength={TITLE_MAX_LENGTH}
                  onChange={(event) => {
                    setTitleDraft(event.target.value)
                  }}
                  placeholder="The build keeps dying halfway through"
                  required
                  value={titleDraft}
                />
                <p className={styles.hint} id={`${fieldId}-title-hint`}>
                  In your own words. Nothing here is matched on vocabulary, so the
                  wording does not have to be anybody else’s.
                </p>
              </div>

              <div className={styles.field}>
                <label className={styles.label} htmlFor={`${fieldId}-details`}>
                  Details
                </label>
                <Textarea
                  id={`${fieldId}-details`}
                  maxLength={DESCRIPTION_MAX_LENGTH}
                  onChange={(event) => {
                    setDescriptionDraft(event.target.value)
                  }}
                  rows={4}
                  value={descriptionDraft}
                />
                <p className={styles.hint}>
                  Optional, and embedded together with the line above — more text usually
                  means a better match, not a narrower one.
                </p>
              </div>

              <div className={styles.formActions}>
                <Button
                  disabled={isLoading || titleDraft.trim() === ''}
                  type="submit"
                  variant="primary"
                >
                  Find similar issues
                </Button>
              </div>
            </form>

            {/* One live region for the outcome, so a screen-reader user hears
                that results arrived without having to go looking. The count
                and not the results themselves: announcing ten rows would be
                worse than announcing none. */}
            <p className={styles.status} role="status">
              {isIdle
                ? 'Describe an issue and search.'
                : isLoading
                  ? 'Searching…'
                  : errorMessage !== null
                    ? 'The search failed.'
                    : `${matches.length} ${matches.length === 1 ? 'issue' : 'issues'} near in meaning.`}
            </p>

            {isLoading && (
              <div aria-hidden="true">
                <Spinner />
              </div>
            )}

            {!isIdle && errorMessage !== null && (
              <ErrorState
                title="Search failed"
                description={errorMessage}
                onRetry={retry}
              />
            )}

            {showResults && matches.length === 0 && (
              /* The one place on this screen where getting the sentence wrong
                 would be a lie. ../lib/indexState.ts owns the decision, and it
                 will only say "nothing is near enough in meaning" when the
                 index is enabled, populated and has nothing waiting. */
              <EmptyState
                icon={<SearchIcon />}
                title={empty.title}
                description={empty.description}
              />
            )}

            {showResults && matches.length > 0 && (
              <section aria-labelledby="semantic-results-title" className={styles.results}>
                <h2 className={styles.resultsTitle} id="semantic-results-title">
                  Nearest in meaning
                </h2>

                <List label="Issues near in meaning">
                  {matches.map(({ issue, similarity }) => {
                    const state = stateById.get(issue.workflowStateId)
                    const category =
                      state === undefined ? null : statusCategoryFrom(state.category)
                    const assignee =
                      issue.assigneeId === null
                        ? undefined
                        : memberById.get(issue.assigneeId)
                    const { name: priorityName } = describePriority(issue.priority)

                    return (
                      <ListRow interactive key={issue.id}>
                        <ListRowMain>
                          {/* A real `<a>`: Tab reaches it, Enter follows it,
                              and "open in new tab" works. The identifier is
                              inside the link so the link's name is "ENG-42 Fix
                              the thing" rather than a title with no address. */}
                          <Link className={styles.rowLink} to={paths.issue(issue.id)}>
                            <span className={styles.identifier}>{issue.identifier}</span>{' '}
                            {issue.title}
                          </Link>
                        </ListRowMain>

                        <ListRowMeta>
                          {/* "similar" and not "match": this is a cosine
                              distance between two embeddings, not a
                              probability that the two are the same issue, and
                              a bare percentage would be read as the second. */}
                          <span className={styles.similarity}>
                            {similarityPercent(similarity)}% similar
                          </span>

                          <PriorityIndicator
                            level={priorityLevel(issue.priority)}
                            name={priorityName ?? String(issue.priority)}
                          />

                          {/* An empty cell rather than a fallback glyph when
                              the state is not resolved: a plausible-looking
                              backlog square for a state we could not resolve
                              is worse than a gap. */}
                          {category !== null && (
                            <StatusIndicator category={category} name={state?.name} />
                          )}

                          {assignee === undefined ? (
                            <VisuallyHidden>Unassigned</VisuallyHidden>
                          ) : (
                            <Avatar name={memberLabel(assignee)} size="sm" />
                          )}
                        </ListRowMeta>
                      </ListRow>
                    )
                  })}
                </List>

                <p className={styles.hint}>
                  Up to ten, above a similarity floor the server sets and a client may not
                  lower. Only issues are indexed by meaning — a project is found by its
                  name, on the ordinary search screen.
                </p>
              </section>
            )}
          </div>

          <div className={styles.column}>
            <IndexPanel
              errorMessage={index.errorMessage}
              isLoading={index.isLoading}
              isRefreshing={refresh.isRefreshing}
              onRefresh={refresh.refresh}
              onRetry={index.retry}
              refreshErrorMessage={refresh.errorMessage}
              state={index.state}
              written={refresh.written}
            />
          </div>
        </div>
      </PageContent>
    </>
  )
}
