/**
 * The application shell's public surface.
 *
 * Two audiences:
 *
 *   - the router, which needs `AppLayout` as its root route element;
 *   - screens, which need `PageHeader` and `PageContent` to sit correctly
 *     inside the shell, and `useRegisterCreateIssueAction` to drive the
 *     sidebar's "New issue" button.
 *
 * Nothing else in `src/app/layout/` is exported. `Sidebar`, `WorkspaceBadge`,
 * `SearchAffordance` and `CreateIssueButton` are internals of the shell and
 * have no meaning anywhere else.
 */

export { AppLayout, MAIN_CONTENT_ID } from './AppLayout'

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
