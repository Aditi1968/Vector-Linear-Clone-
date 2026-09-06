import { Outlet } from 'react-router-dom'

import { CreateIssueActionProvider } from './CreateIssueActionProvider'
import { Sidebar } from './Sidebar'
import styles from './AppLayout.module.css'

/**
 * The id the skip link targets and `<main>` carries.
 *
 * Exported so that a test, or anything that later needs to move focus to the
 * content region, refers to the same string this file does rather than
 * repeating a fragment that only breaks when someone renames it here.
 */
export const MAIN_CONTENT_ID = 'main-content'

/**
 * Vector's application shell: persistent rail on the left, page content on
 * the right.
 *
 * Mounted as the router's root route element, so it renders once and survives
 * every navigation. That is not just an optimisation -- chrome that unmounts
 * and remounts loses focus position and scroll position on every route
 * change, which is the difference between an application and a website.
 *
 * Three things are established here and nowhere else:
 *
 *   - the `main` landmark, and the skip link that reaches it;
 *   - the create-issue slot (see ./createIssueAction.ts), wrapped around both
 *     the sidebar that reads it and the `<Outlet />` that fills it;
 *   - the two-column geometry, in AppLayout.module.css.
 *
 * The skip link is the first focusable element in the document, which is the
 * only position it works from.
 *
 * `<main>` carries `tabIndex={-1}`. Without it, following the skip link
 * updates the URL fragment and scrolls, but leaves focus on the link, so the
 * next Tab lands back at the top of the sidebar -- the exact loop the link
 * exists to break. `-1` makes the element programmatically focusable without
 * adding it to the tab order.
 */
export function AppLayout() {
  return (
    <CreateIssueActionProvider>
      <div className={styles.shell}>
        <a className={styles.skipLink} href={`#${MAIN_CONTENT_ID}`}>
          Skip to main content
        </a>

        <Sidebar />

        <main id={MAIN_CONTENT_ID} className={styles.main} tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </CreateIssueActionProvider>
  )
}
