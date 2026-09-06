import { Link } from 'react-router-dom'

import { PageContent, PageHeader } from '../layout'
import { useAppPaths } from './useAppPaths'

/**
 * The screen for a URL that matches no route.
 *
 * It is a route element rather than a thrown error so that an unknown URL
 * still renders inside the shell: the sidebar stays put, and the address bar
 * keeps the URL that missed, which is what makes a mistyped link
 * recoverable rather than a dead end.
 *
 * `AppLayout` owns the `<main>` landmark and `PageHeader` owns the page's
 * only `<h1>`, so this renders a fragment and adds neither. A second `<main>`
 * here would give the document two "main" landmarks -- invalid HTML, and a
 * screen reader's "skip to main content" would land in the wrong one.
 *
 * The link back is built from `useAppPaths()` rather than a literal
 * `/issues`, for the same reason every other link in the app is: when a
 * `/:workspaceSlug` segment arrives after backend Phase 1b-5, the path
 * helper changes and this file does not.
 */
export function NotFound() {
  const paths = useAppPaths()

  return (
    <>
      <PageHeader
        title="Page not found"
        description="That URL does not match anything in Vector."
      />

      <PageContent constrained>
        <p>
          The page may have been moved, or the address may have been mistyped.
        </p>

        <p>
          <Link to={paths.issues()}>Back to issues</Link>
        </p>
      </PageContent>
    </>
  )
}
