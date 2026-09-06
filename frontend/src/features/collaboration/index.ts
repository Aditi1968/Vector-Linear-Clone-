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
 * design and it is not an interface proposal to negotiate over -- the
 * collaboration branch's own `index.ts` replaces this file wholesale, and
 * the add/add conflict at merge time is the intended signal: take theirs.
 *
 * The only thing this branch has committed to is the shape of the call:
 *
 *     <IssueLabelsPanel issueId={issue.id} />
 *     <IssueSubIssuesPanel issueId={issue.id} />
 *     <IssueRelationsPanel issueId={issue.id} />
 *     <IssueCommentsPanel issueId={issue.id} />
 *
 * and two conventions that come with the slot:
 *
 *   - The workspace is NOT passed. Every hook in this application reads it
 *     from the route with `useWorkspaceSlug()`, because a prop would let two
 *     components on one page disagree about which tenant they are showing.
 *   - Each panel renders its own heading, at level 3. The page's `<h1>` is
 *     "Issues" and the inspector's `<h2>` is the issue title, so a panel
 *     heading at any other level breaks the document outline.
 *
 * If the real panels need a different signature, change the four call sites
 * in IssueInspector.tsx; nothing else in the issues feature imports this.
 */

export interface CollaborationPanelProps {
  /** The issue the panel is about. A UUID, as `Issue.id`. */
  issueId: string
}

/** Labels attached to this issue, and the control to attach more. */
export function IssueLabelsPanel(_props: CollaborationPanelProps) {
  return null
}

/** This issue's children, and the control to add one. */
export function IssueSubIssuesPanel(_props: CollaborationPanelProps) {
  return null
}

/** Blocks / blocked by / related / duplicate. */
export function IssueRelationsPanel(_props: CollaborationPanelProps) {
  return null
}

/** The comment thread. */
export function IssueCommentsPanel(_props: CollaborationPanelProps) {
  return null
}
