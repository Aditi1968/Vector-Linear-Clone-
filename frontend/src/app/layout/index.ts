/**
 * The application shell's public surface.
 *
 * Two audiences:
 *
 *   - the router, which needs `AppLayout` as the element of the
 *     `/:workspaceSlug` route;
 *   - screens, which need `PageHeader` and `PageContent` to sit correctly
 *     inside the shell, and `useRegisterCreateIssueAction` to drive the
 *     rail's "New issue" button.
 *
 * Nothing else in `src/app/layout/` is exported. `Sidebar`,
 * `WorkspaceSwitcher`, `AccountMenu`, the shell's action affordances and its
 * pre-workspace states are internals of the shell and have no meaning
 * anywhere else. The workspace a screen is inside is published separately,
 * from `src/app/workspace`.
 */

export { AppLayout, MAIN_CONTENT_ID, SIDEBAR_NAV_ID } from './AppLayout'

export { PageHeader } from './PageHeader'
export type { PageHeaderProps } from './PageHeader'

export { PageContent } from './PageContent'
export type { PageContentProps } from './PageContent'

export { useRegisterCreateIssueAction, useCreateIssueAction } from './createIssueAction'
export type {
  CreateIssueHandler,
  RegisterCreateIssueAction,
} from './createIssueAction'

export { CreateIssueActionProvider } from './CreateIssueActionProvider'

/* The chord the command palette will be bound to, exported so that whoever
 * binds it and the hint that advertises it agree on how it is spelled. */
export { COMMAND_CHORD } from './ShellActions'
