import { useEffect, useId, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { PageContent, PageHeader } from '../../app/layout'
import { useAppPaths } from '../../app/routes'
import {
  Avatar,
  Badge,
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
  VisuallyHidden,
  statusCategoryFrom,
} from '../../components'
import { memberLabel, useWorkspaceContext } from '../issues/api'
import { describePriority, priorityLevel } from '../issues/lib/priority'
import { formatDay, projectStateLabel, projectStateTone } from '../projects/lib/projects'
import styles from '../screens.module.css'
import { useSearch } from './api'

/** The URL parameter the query lives in. `?q=` is what a user expects to see. */
const QUERY_PARAM = 'q'

/**
 * How long the typing has to stop before the URL -- and so the request --
 * moves.
 *
 * Long enough that a word typed at speed is one request rather than six,
 * short enough not to feel like waiting. It debounces the *URL*, not the
 * query: the input is uncontrolled by the network and never lags a keystroke.
 */
const DEBOUNCE_MS = 250

/**
 * Search this workspace's issues and projects.
 *
 * ## The query lives in the URL
 *
 * `?q=ENG-42`, so a search is a link: it survives a refresh, it can be sent
 * to someone, and the browser's back button returns to the previous one.
 * Keeping it in component state instead would make every search a thing you
 * have to retype, and would leave the command palette with no way to open
 * this screen on a query.
 *
 * The address bar is written with `replace: true` while typing, so a
 * six-letter word leaves one history entry rather than six -- otherwise
 * "back" would walk the user through their own keystrokes.
 *
 * The two directions are kept apart deliberately. Typing pushes the draft
 * into the URL after the debounce; a URL that changes for any *other* reason
 * -- the back button, a pasted link, the palette navigating here -- pushes
 * back into the draft. `pushedRef` is what tells them apart, and without it
 * the second case silently does nothing and the input shows a stale query
 * beside fresh results.
 *
 * ## The string is passed through untouched
 *
 * The backend resolves an identifier directly, so typing "ENG-42" finds that
 * issue. Nothing here uppercases it, strips the hyphen or splits it into
 * terms; the only transformation is trimming whitespace to decide whether a
 * query exists at all.
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`; this renders a fragment and adds neither.
 */
export function SearchPage() {
  const paths = useAppPaths()
  const [params, setParams] = useSearchParams()
  const query = params.get(QUERY_PARAM) ?? ''

  const [draft, setDraft] = useState(query)

  /** The last value this screen itself put in the URL. See the note above. */
  const pushedRef = useRef(query)

  const inputId = useId()

  // The URL changed from somewhere that is not this input. Adopt it.
  useEffect(() => {
    if (query !== pushedRef.current) {
      pushedRef.current = query
      setDraft(query)
    }
  }, [query])

  // The input changed. Move the URL after the typing stops.
  useEffect(() => {
    if (draft === query) {
      return
    }

    const timer = setTimeout(() => {
      pushedRef.current = draft

      setParams(
        (current) => {
          const next = new URLSearchParams(current)

          // Removed rather than set to empty: `/search` and `/search?q=` are
          // the same screen and only one of them is a URL worth having.
          if (draft === '') {
            next.delete(QUERY_PARAM)
          } else {
            next.set(QUERY_PARAM, draft)
          }

          return next
        },
        { replace: true },
      )
    }, DEBOUNCE_MS)

    return () => {
      clearTimeout(timer)
    }
  }, [draft, query, setParams])

  const { issues, projects, isIdle, isLoading, errorMessage, retry } = useSearch(query)

  // For drawing a status glyph and an avatar from the UUIDs a result carries.
  // A failure costs those two columns and nothing else.
  const { stateById, memberById } = useWorkspaceContext()

  const total = issues.length + projects.length
  const showResults = !isIdle && !isLoading && errorMessage === null

  return (
    <>
      <PageHeader
        title="Search"
        description="Issues and projects in this workspace."
      />

      <PageContent>
        <div className={styles.stack}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={inputId}>
              Search
            </label>
            <Input
              id={inputId}
              // `type="search"` for the clear affordance browsers give it and
              // for the keyboard it brings up on a phone.
              type="search"
              autoComplete="off"
              // The one place in the product where taking focus on mount is
              // right: the user navigated to a screen whose entire purpose is
              // this field.
              autoFocus
              icon={<SearchIcon />}
              placeholder="Title, description, or an identifier like ENG-42"
              value={draft}
              onChange={(event) => {
                setDraft(event.target.value)
              }}
            />
          </div>

          {/* One live region for the outcome, so a screen-reader user hears
              that results arrived without having to go looking. It is the
              count and not the results themselves: announcing twenty rows
              would be worse than announcing none. */}
          <p className={styles.footnote} role="status">
            {isIdle
              ? 'Type to search.'
              : isLoading
                ? 'Searching...'
                : errorMessage !== null
                  ? 'Search failed.'
                  : `${total} ${total === 1 ? 'result' : 'results'} for "${query}".`}
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

          {showResults && total === 0 && (
            <EmptyState
              icon={<SearchIcon />}
              title="Nothing matched"
              description={`No issue or project in this workspace matches "${query}".`}
            />
          )}

          {showResults && issues.length > 0 && (
            <section className={styles.panel} aria-labelledby="search-issues">
              <div className={styles.panelHeader}>
                <h2 className={styles.panelTitle} id="search-issues">
                  Issues
                </h2>
                <span className={styles.footnote}>{issues.length}</span>
              </div>

              <div className={styles.panelBody}>
                <List label="Matching issues">
                  {issues.map((issue) => {
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
                          {/* A real `<a>` stretched over the row: Tab reaches
                              it, Enter follows it, and "open in new tab"
                              works. The identifier is inside the link so the
                              link's name is "ENG-42 Fix the thing" rather
                              than a title with no address. */}
                          <Link className={styles.rowLink} to={paths.issue(issue.id)}>
                            <span className={styles.identifier}>
                              {issue.identifier}
                            </span>{' '}
                            {issue.title}
                          </Link>
                        </ListRowMain>

                        <ListRowMeta>
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
              </div>
            </section>
          )}

          {showResults && projects.length > 0 && (
            <section className={styles.panel} aria-labelledby="search-projects">
              <div className={styles.panelHeader}>
                <h2 className={styles.panelTitle} id="search-projects">
                  Projects
                </h2>
                <span className={styles.footnote}>{projects.length}</span>
              </div>

              <div className={styles.panelBody}>
                <List label="Matching projects">
                  {projects.map((project) => (
                    <ListRow interactive key={project.id}>
                      <ListRowMain>
                        <Link className={styles.rowLink} to={paths.project(project.id)}>
                          {project.name}
                        </Link>
                      </ListRowMain>

                      <ListRowMeta>
                        {project.targetDate !== null && (
                          <span className={styles.rowSub}>
                            {/* The word matters: a bare date beside a project
                                could be a start, an end or a last edit. */}
                            Target{' '}
                            <time dateTime={project.targetDate}>
                              {formatDay(project.targetDate)}
                            </time>
                          </span>
                        )}
                        <Badge tone={projectStateTone(project.state)}>
                          {projectStateLabel(project.state)}
                        </Badge>
                      </ListRowMeta>
                    </ListRow>
                  ))}
                </List>
              </div>
            </section>
          )}

          {showResults && total > 0 && (
            // `search` returns two plain lists and no `pageInfo`, so there is
            // no next page to ask for. Said out loud rather than implied: a
            // list that stops at twenty without explaining looks like a list
            // that ends at twenty.
            <p className={styles.footnote}>
              The closest matches, up to twenty of each. Narrow the search to
              see different ones.
            </p>
          )}
        </div>
      </PageContent>
    </>
  )
}
