import { EmptyState } from '../../components'
import { PageContent, PageHeader } from '../layout'

export interface PlaceholderProps {
  /** The screen's name, as the sidebar entry that reaches it spells it. */
  title: string
  /** What will be here, in one sentence. Never a promise about when. */
  description: string
}

/**
 * A route that exists and a screen that does not, said plainly.
 *
 * The shell's navigation is built from the product's real surface -- the
 * backend has issues, projects, cycles, notifications and search -- and the
 * screens for most of them belong to other people. The alternative to this
 * component is a sidebar entry that leads nowhere, which is worse in both
 * directions: a dead link for a user, and a missing mount point for whoever
 * builds the screen.
 *
 * So the entry navigates, the URL is the real one from `paths`, the shell
 * stays put, and the page says there is nothing here yet. Nothing on it
 * pretends to be data.
 *
 * ## For whoever builds the screen
 *
 * Replace the `element` in ./routes.tsx with your page. Render `PageHeader`
 * and `PageContent` and no `<main>` of your own -- `AppLayout` owns that
 * landmark and `PageHeader` owns the page's only `<h1>`. Take the workspace
 * from `useWorkspaceSlug()` for query variables and from `useWorkspace()`
 * for anything you display, and build every link with `useAppPaths()`.
 */
export function Placeholder({ title, description }: PlaceholderProps) {
  return (
    <>
      <PageHeader title={title} />

      <PageContent constrained>
        <EmptyState title="Not built yet" description={description} />
      </PageContent>
    </>
  )
}
