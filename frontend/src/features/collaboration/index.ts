/**
 * Collaboration on an issue: comments, labels, relations and sub-issues.
 *
 * Four self-contained panels, meant to be mounted inside an issue detail view
 * that passes two ids and nothing else:
 *
 *     import {
 *       CommentsPanel,
 *       LabelsPanel,
 *       RelationsPanel,
 *       SubIssuesPanel,
 *     } from '../../features/collaboration'
 *
 *     <LabelsPanel workspaceSlug={slug} issueId={id} />
 *     <SubIssuesPanel workspaceSlug={slug} issueId={id} />
 *     <RelationsPanel workspaceSlug={slug} issueId={id} />
 *     <CommentsPanel workspaceSlug={slug} issueId={id} />
 *
 * Every one of them takes exactly `{ workspaceSlug, issueId }`, both required
 * strings, and owns everything else: its own queries, its own loading,
 * empty, and error states, its own mutations and its own cache updates. There
 * is no context to provide, no data to thread down, and no callback to wire
 * up -- a panel that needed the host to refetch something would not be
 * self-contained, it would be a component with a manual attached.
 *
 * ## What the host has to provide
 *
 * A router (the panels build issue links through `useAppPaths()`) and an
 * Apollo client built with `createCache()` from `src/lib/graphql` -- the
 * connection field policies live there, and without them "load more" writes
 * pages into a cache field nothing is watching. `src/app/providers` and
 * `src/test/render.tsx` both already do this.
 *
 * No `ToastProvider` is required. Each panel announces its own results
 * through a `role="status"` region it renders itself, so mounting one does
 * not oblige the host to have set up a toast region first.
 *
 * ## Heading level
 *
 * Each panel is a `<section>` titled by an `<h2>`. They are top-level
 * sections of a view whose issue title is the `<h1>`.
 *
 * ## Comments cannot be edited
 *
 * The schema has `commentCreate` and `commentDelete` and no update. There is
 * no edit affordance here and no "edited" marker, because nothing on the
 * server sets `Comment.editedAt`. Do not add one to the host view either.
 */

export { CommentsPanel } from './components/CommentsPanel'
export type { CommentsPanelProps } from './components/CommentsPanel'

export { LabelsPanel } from './components/LabelsPanel'
export type { LabelsPanelProps } from './components/LabelsPanel'

export { RelationsPanel } from './components/RelationsPanel'
export type { RelationsPanelProps } from './components/RelationsPanel'

export { SubIssuesPanel } from './components/SubIssuesPanel'
export type { SubIssuesPanelProps } from './components/SubIssuesPanel'
