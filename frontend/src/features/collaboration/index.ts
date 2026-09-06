/**
 * PLACEHOLDER -- owned by the collaboration agent, not by this branch.
 *
 * ================================================================
 * DELETE THIS FILE when the real `features/collaboration` lands.
 * ================================================================
 *
 * The issue inspector (`features/issues/components/IssueInspector.tsx`)
 * mounts four panels it does not own: labels, sub-issues, relations and
 * comments. Those are being built in parallel and do not exist on this
 * branch yet, so this module stands in for them and renders nothing at all.
 *
 * It exists so that this branch compiles and its gates run. It is not a
 * design and it is not an interface to negotiate over -- the collaboration
 * branch's own `index.ts` replaces this file wholesale, and the add/add
 * conflict at merge time is the intended signal: take theirs.
 *
 * The call the inspector makes, which is the whole of what this branch has
 * committed to:
 *
 *     <LabelsPanel    issueId={issue.id} workspaceSlug={workspaceSlug} />
 *     <SubIssuesPanel issueId={issue.id} workspaceSlug={workspaceSlug} />
 *     <RelationsPanel issueId={issue.id} workspaceSlug={workspaceSlug} />
 *     <CommentsPanel  issueId={issue.id} workspaceSlug={workspaceSlug} />
 *
 * One convention comes with the slot: each panel renders its own heading, at
 * level 3. The page's `<h1>` is "Issues" and the inspector's `<h2>` is the
 * issue title, so a panel heading at any other level breaks the outline.
 *
 * If the real panels need a different signature, change the four call sites
 * in IssueInspector.tsx; nothing else in the issues feature imports this.
 */

export interface CollaborationPanelProps {
  /** The issue the panel is about. A UUID, as `Issue.id`. */
  issueId: string
  /**
   * The workspace from the route.
   *
   * Passed even though every hook in this application can read it from the
   * route itself, because the collaboration branch asked for it explicitly.
   * The inspector reads it through `useWorkspaceSlug()` and hands the same
   * value down, so the two cannot disagree about which tenant is on screen.
   */
  workspaceSlug: string
}

/**
 * A panel, typed by what it accepts rather than by what these stubs use.
 *
 * The stubs take no argument at all -- a parameter they ignore is an
 * unused-variable error, and naming it `_props` only moves the argument to
 * the lint config -- but the *type* still declares the two props, so a call
 * site that passes the wrong thing fails here rather than when the real
 * panels land.
 */
type CollaborationPanel = (props: CollaborationPanelProps) => null

/** Labels attached to this issue, and the control to attach more. */
export const LabelsPanel: CollaborationPanel = () => null

/** This issue's children, and the control to add one. */
export const SubIssuesPanel: CollaborationPanel = () => null

/** Blocks / blocked by / related / duplicate. */
export const RelationsPanel: CollaborationPanel = () => null

/** The comment thread. */
export const CommentsPanel: CollaborationPanel = () => null
