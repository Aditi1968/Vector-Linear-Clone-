# Vector button/control inventory

Every interactive control in the signed-in product, grouped by route, mapped
to the GraphQL operation it fires and the result a test can observe. Compiled
by reading `frontend/src` (not by running the app). Columns:

**Control (accessible name)** · **file:line** · **GraphQL op** · **Observable result** · **Flags**

Flags: `OPENS-MENU` (local popover/dialog, no backend) · `DISABLED-ONLY`
(disabled until a field is filled / while in-flight) · `NO-HANDLER` ·
`NO-NAME` · `NAV` (client navigation only) · `EXTERNAL` (leaves the SPA).

## Static findings (before any test runs)

- **No `NO-NAME` controls** anywhere. Every icon-only button carries an
  `aria-label`; every `Menu`/`IconButton` has an explicit `label`.
- **One `NO-HANDLER`:** the revision-preview `<details>/<summary>` in
  `DocumentPanel.tsx:241` — an intentional native disclosure, not a dead
  button. The single-workspace identity in `WorkspaceSwitcher.tsx:44` is a
  `<div>` by design (only one workspace → nothing to switch to).
- So the static pass found **no broken controls**. "Dead/fails" findings, if
  any, can only surface at runtime (Phase 3) — a wired button whose mutation
  errors on staging.

## Global rules for Phase 3 tests (safety + determinism)

1. **Unique data per test.** Every test mints its own records named
   `e2e <ISO-timestamp>` (e.g. `e2e 2026-11-10T12:34:56.789Z`). Assert on
   *that* record (by its unique name/identifier), never on list counts,
   "first row", or another test's data.
2. **Never touch data outside `e2e-playground`.** All tests run in that one
   workspace.
3. **Logout is isolated.** `Logout` deletes the session row keyed by the
   shared cookie (`SessionRepository.delete_by_token_hash`). A logout on the
   shared `storageState` would sign out every other parallel test. The
   logout test MUST open its own context, log in fresh (a second session),
   then log out. Do not use the shared session for it.
4. **GitHub/Slack settings** require a connected install on staging. If
   disconnected, the automation/repo controls don't render — those tests
   must `test.skip()` when the Connect link is showing, not fail.
5. **Members:** invite + revoke are safe (stay in-workspace). Role-change and
   remove need a second member or hit the "last owner" refusal — test the
   refusal (observable, safe) rather than removing the only owner.
6. Use `expectMutation(page, '<OpName>')` (e2e/helpers.ts) around every click
   that fires a mutation: create the waiter before the click, await after.

---

# Group: shell (all routes) → `shell.spec.ts`

Chrome mounted by `AppLayout` on every workspace route. Nav links and command
"Go to" entries are `NAV` only (destination screen runs its own query).

| Control | file:line | GraphQL op | Observable result | Flags |
|---|---|---|---|---|
| "Sign out" (Account menu item) | AccountMenu.tsx:62 | **Logout** | Logout fires; redirect to landing; Apollo store cleared | isolate (rule 3) |
| "Account" (menu trigger) | AccountMenu.tsx:89 | — | Account dropdown opens | OPENS-MENU |
| "Workspace settings" (menu item) | AccountMenu.tsx:54 | — | → /:slug/settings | NAV |
| "New issue" (shell button) | CreateIssueButton.tsx:46 | — (opens composer → `IssueCreate` on submit) | Composer opens | DISABLED-ONLY (no composer registered) |
| "Command" (⌘K) | ShellActions.tsx:84 | — | Command palette opens | OPENS-MENU |
| "Search" | ShellActions.tsx:28 | — | → /:slug/search | NAV |
| "Collapse/Expand sidebar" | Sidebar.tsx:52 | — | Rail toggles; persisted pref | — |
| "Switch workspace" + items | WorkspaceSwitcher.tsx:68 | — | → /:otherslug | NAV, OPENS-MENU (≥2 workspaces only) |
| 18 nav links (My Issues, Inbox, Triage, All Issues, Board, Projects, Initiatives, Roadmap, Documents, Saved Views, Favorites, Members, Analytics, Releases, Environments, Label Groups, Templates, Settings) | navigationItems.tsx:67–203 | — | Navigate; `aria-current` on active | NAV |
| Team disclosure + team/issues/cycles sub-links | SidebarNav.tsx:208–250 | — | Expand/collapse; navigate | NAV |
| Command palette: "New issue", "Keyboard shortcuts", 8 "Go to" entries | CommandPalette/commands.tsx | — | open composer / view / navigate | NAV / OPENS-MENU |
| Command palette search input + issue/project result rows | useCommandSearch.tsx:90 | **CommandSearch** (query) | Results listed; clicking a row navigates | NAV |

**Test targets:** Logout (isolated context), command-palette `CommandSearch`
(type a query, assert results), a representative nav link sets `aria-current`.

---

# Group: issues cluster → `issues.spec.ts`, `board.spec.ts`, `triage.spec.ts`, `inbox.spec.ts`

## /:slug/issues — list + composer + inspector

| Control | file:line | GraphQL op | Observable result | Flags |
|---|---|---|---|---|
| "New issue" → "Create issue" (submit) | IssueComposer.tsx:244 | **IssueCreate** | New row prepended; composer closes | |
| "Cancel" (composer) | IssueComposer.tsx:247 | — | Composer closes | |
| "Create the first issue" | ListStates.tsx:85 | — | Opens composer | empty state |
| "Load more" | IssuesScreen.tsx:385 | **IssueList** (fetchMore) | Next page appended | |
| "Retry" (list error) | IssuesScreen.tsx:299 | **IssueList** (refetch) | List reloads | |
| Row density / Group-by radios | ViewControls.tsx:62,71 | — | Client-only view change | |
| Issue row link | IssueRow.tsx:95 | — | → /issues/:id | NAV |
| Title / Description (EditableText) | IssueInspector.tsx:353,391 | **IssueUpdate** | Field saved on blur/Enter | |
| Status / Priority / Assignee (combobox) | IssueInspector.tsx:483,514,544 | **IssueUpdate** | Field changes | |
| Estimate / Due date (EditableText) | IssueInspector.tsx:571,597 | **IssueUpdate** | Saved (blank→null) | |
| Project (combobox) | IssueInspector.tsx:615 | **IssueSetProject** | Project changes | |
| Cycle (combobox) | IssueInspector.tsx:637 | **IssueSetCycle** | Cycle changes | |
| "More actions" → "Archive issue" | IssueInspector.tsx:316,319 | **IssueArchive** | Row removed; inspector closes | OPENS-MENU |
| "Close issue" | IssueInspector.tsx:329 | — | → /issues | NAV |

**Test targets:** IssueCreate, IssueUpdate (status + title), IssueSetProject,
IssueSetCycle, IssueArchive, IssueList fetchMore.

## /:slug/board — `board.spec.ts`

| Control | file:line | GraphQL op | Observable result | Flags |
|---|---|---|---|---|
| Team combobox | BoardControls.tsx:129 | **BoardIssues** (refetch) | Columns+cards reload | |
| Sort / Status / Assignee / Label / Priority / Project / Cycle comboboxes | BoardControls.tsx:175–251 | **BoardIssues** (new filter/order) | Board reorders/filters | some DISABLED-ONLY |
| Group-by combobox | BoardControls.tsx:149 | — | Client regroup | |
| "Clear filters" | BoardControls.tsx:256 | **BoardIssues** | Full board returns | active-filter only |
| Card "Move {id}" menu → state item | BoardCard.tsx:195,113 | **BoardIssueMove** | Card moves column (optimistic) | OPENS-MENU |
| Column drop target (drag/drop) | BoardColumn.tsx:81 | **BoardIssueMove** | Card moves on drop | not a click |
| "Load more" / retries | BoardScreen.tsx:335,390 | **BoardIssues** | More cards / reload | |

**Test targets:** BoardIssueMove (via the card Move menu — deterministic,
avoids drag/drop flakiness), a BoardIssues filter (Status) changes the set.

## /:slug/triage — `triage.spec.ts`

| Control | file:line | GraphQL op | Observable result | Flags |
|---|---|---|---|---|
| Team combobox | TriagePage.tsx:245 | **TriageQueue** (refetch) | Queue reloads | |
| "Accept" menu → state | TriageRow.tsx:211,90 | **TriageAccept** | Issue accepted, leaves queue | OPENS-MENU |
| "More actions" → "Decline" | TriageRow.tsx:229,105 | **TriageDecline** | Issue declined, leaves queue | OPENS-MENU |
| → "Mark as duplicate…" → "Mark as duplicate" | TriageRow.tsx:116 / TriagePage.tsx:386 | **TriageMarkDuplicate** | Closed as dup, leaves queue | DISABLED-ONLY until target |
| → priority / assignee / unassign items | TriageRow.tsx:126,138,156 | **TriageIssueUpdate** | Field set; refetched | |
| → "Move to {teamKey}" | TriageRow.tsx:166 | **TriageChangeTeam** | Moves to other queue | needs 2nd team |
| Row title link | TriageRow.tsx:198 | — | → /issues/:id | NAV |

**Test targets:** create an issue in a triage state, then TriageAccept /
TriageDecline / TriageIssueUpdate (priority). TriageChangeTeam needs a second
team (QA is the only one) → inventory-only unless a 2nd team is created.

## /:slug/inbox — `inbox.spec.ts`

| Control | file:line | GraphQL op | Observable result | Flags |
|---|---|---|---|---|
| "Mark all read" | InboxPage.tsx:186 | **NotificationMarkAllRead** | All read; badge→0 | DISABLED-ONLY when 0 unread |
| "Mark read: {sentence}" (per unread row) | InboxPage.tsx:361 | **NotificationMarkRead** | Row read; dot clears | unread rows only |
| "All" / "Unread" radios | InboxPage.tsx:179 | **NotificationInbox** (unreadOnly) | List switches | |
| "Load more" | InboxPage (list) | **NotificationInbox** (fetchMore) | More rows | |
| Row link | InboxPage.tsx:313 | — | → /issues/:id | NAV |

**Note:** inbox needs a notification to exist. Notifications are produced by
workspace activity (assignments/comments). With a single-member workspace,
the inbox may be empty → mark-read tests must create a triggering event first
or `test.skip()` when empty. **Test targets:** NotificationMarkAllRead (skip
if 0 unread), filter radios switch query.

## /:slug/my-issues
Navigation/list only: "Load more" (**MyIssues** fetchMore), "Try again"
(refetch), row links. No mutations. **Inventory-only** (covered by issues).

---

# Group: collaboration (issue detail panels) → `collaboration.spec.ts`

Rendered on /:slug/issues/:issueId. 9 mutations, 7 queries.

| Control | file:line | GraphQL op | Observable result | Flags |
|---|---|---|---|---|
| "Comment" (submit) | CommentsPanel.tsx:207 | **CommentCreate** | Comment appears; textarea clears | |
| "Delete comment by {n}" → dialog "Delete comment" | CommentsPanel.tsx:140,231 | **CommentDelete** | Comment removed | OPENS-MENU → confirm |
| "Load more comments" | CommentsPanel.tsx:168 | **IssueComments** (fetchMore) | More comments | |
| "Add" (label) → label menu item | LabelsPanel.tsx:128,63 | **IssueLabelAttach** | Chip added | OPENS-MENU |
| "Remove {label}" (chip) | Tag via LabelsPanel.tsx:152 | **IssueLabelDetach** | Chip removed | |
| "New label" → "Create" | LabelsPanel.tsx:187 | **LabelCreate** then **IssueLabelAttach** | Label created + attached | |
| Relations "Add" → picker result | RelationsPanel.tsx:116 / IssuePicker.tsx:129 | **IssueRelationCreate** | Relation row added | OPENS-MENU |
| "Remove {rel} {title}" | RelationsPanel.tsx:169 | **IssueRelationDelete** | Relation removed | |
| "This issue is" (relation-type select) | RelationsPanel.tsx:234 | — | Sets type for next pick | |
| Sub-issues "Add"/"Set parent" → picker | SubIssuesPanel.tsx:85,102 | **IssueSetParent** | Parent set / child added | OPENS-MENU |
| "Remove … parent/sub-issue" | SubIssuesPanel.tsx:118,151 | **IssueClearParent** | Parent/child cleared | |
| IssuePicker "Search issues" | IssuePicker.tsx:83 | **IssueSearch** (debounced) | Result list populates | |
| Panel retry (each panel) | Panel.tsx:86 | per-panel query refetch | Panel reloads | error state |

**Test targets:** CommentCreate + CommentDelete, LabelCreate+Attach +
IssueLabelDetach, IssueRelationCreate+Delete, IssueSetParent+ClearParent
(each creates its own helper issue(s) to link).

---

# Group: planning → `projects.spec.ts`, `cycles.spec.ts`, `initiatives.spec.ts`, `releases.spec.ts`

## /:slug/projects (+ /:projectId)

| Control | file:line | GraphQL op | Observable result | Flags |
|---|---|---|---|---|
| "New project" → "Create project" | ProjectForm.tsx:208 | **ProjectCreate** | New row at head; dialog closes | DISABLED-ONLY (name) |
| "Actions" → "Edit project" → "Save changes" | ProjectForm.tsx:208 | **ProjectUpdate** | Project saved | OPENS-MENU |
| "Actions" → "Delete project" | ProjectDetailPage.tsx:259 | **ProjectDelete** | Deleted; → list | OPENS-MENU |
| "Load more" / retries | ProjectListPage.tsx:247,166 | **ProjectList** | More / reload | |
| "Add milestone" / milestone actions | ProjectMilestones.tsx:176+ | **ProjectMilestoneCreate / Update / Delete** | Milestone row changes | |
| Project "Actions" extras | — | **ProjectTeamAdd / ProjectTeamRemove** | Team (de)associated | OPENS-MENU |
| Row link | ProjectListPage.tsx:198 | — | → /projects/:id | NAV |

## /:slug/cycles (+ /:cycleId), /:slug/initiatives, /:slug/releases, /:slug/roadmap
- **Cycles** (team-scoped — needs a `teamId`, QA works): **CycleCreate**,
  **CycleUpdate**, **CycleDelete**.
- **Initiatives**: **InitiativeCreate**, **InitiativeUpdate**,
  **InitiativeDelete**, plus **InitiativeProjectAdd/Remove**,
  **InitiativeSetParent/ClearParent**, **InitiativeUpdatePost**.
- **Releases**: **ReleaseCreate**, **ReleaseStatusSet** (status change, not
  "publish"), **ReleaseDelete**.
- **Roadmap**: a view over initiatives+projects — `NAV`/filter only.

**Test targets:** ProjectCreate→ProjectUpdate→ProjectDelete (full lifecycle on
one `e2e <ts>` project); CycleCreate; InitiativeCreate; ReleaseCreate.

---

# Group: admin → `admin.spec.ts`

## /:slug/members

| Control | file:line | GraphQL op | Observable result | Flags |
|---|---|---|---|---|
| "Create invite link" (submit) | MembersPage.tsx:482 | **MemberInvitationCreate** | Invite + link appended | |
| "Revoke the invitation for {email}" | MembersPage.tsx:560 | **MemberInvitationRevoke** | Invitation withdrawn | |
| "Copy invite link for {email}" | MembersPage.tsx:509 | — (clipboard) | Status toast | |
| "Actions for {m}" → "Make {role}" | MembersPage.tsx:283 | **MemberRoleUpdate** | Role changed | needs 2nd member |
| → "Remove from workspace" → "Remove" | MembersPage.tsx:607 | **MemberRemove** | Member removed / **refused for last owner** | confirm dialog |

**Test targets (safe):** MemberInvitationCreate → MemberInvitationRevoke
(round-trip on an `e2e+<ts>@example.com` invite). Role/Remove →
inventory-only (single member); optionally assert the last-owner refusal.

## /:slug/environments / label-groups / templates

| Control | GraphQL op | Observable result |
|---|---|---|
| "New environment" → "Add environment" | **EnvironmentCreate** | Env row added |
| "New group" → "Create group" / "Save changes" | **LabelGroupCreate** / **LabelGroupUpdate** | Group added/updated |
| "Delete group" | **LabelGroupDelete** | Group removed (labels ungrouped) |
| "Add to group" / "Remove {label}" | **LabelSetGroup** | Label (un)grouped |
| "New template" → "Create template"/"Save changes" | **IssueTemplateCreate**/**Update** | Template added/updated |
| "Delete template" | **IssueTemplateDelete** | Template removed |
| "New issue from {t}" → "Create issue" | **IssueCreateFromTemplate** | Issue filed; success link |

**Test targets:** EnvironmentCreate; LabelGroupCreate→Delete;
IssueTemplateCreate→ IssueCreateFromTemplate→IssueTemplateDelete.

## /:slug/settings
GitHub/Slack **Connect** are `<a href>` OAuth (EXTERNAL, no GraphQL). When
connected: tracked-repo checkbox (**WorkspaceGithubRepositoriesSet**), PR
automation enable + 2 state selects (**WorkspaceGithubAutomationSet**),
Disconnect (**WorkspaceGithubDisconnect**/**WorkspaceSlackDisconnect**).
**Requires a connected install → `test.skip()` when disconnected.**

---

# Group: views → `views.spec.ts`

## /:slug/saved-views
"New view"→"Create view" **SavedViewCreate**; "Edit view…"→"Save changes"
**SavedViewUpdate**; "Delete view" **SavedViewDelete**; selecting a row runs
**SavedViewResults**. **Targets:** SavedViewCreate→Update→Delete.

## /:slug/favorites
"Add a favorite"→item **FavoriteAdd**; "Remove from favorites"
**FavoriteRemove**; "Move up/down" **FavoriteReorder**. **Targets:**
FavoriteAdd→FavoriteRemove (favorite the QA team, then unfavorite).

## /:slug/documents
"New document"→"Create document" **DocumentCreate**; "Rename"/"Save body"
**DocumentEdit**; "Restore the version…" **DocumentRestore**; "Comment"
**DocumentCommentCreate**; "Delete the comment…" **DocumentCommentDelete**;
"Delete document" **DocumentDelete**. `<details>` preview = NO-HANDLER
(intentional). **Targets:** DocumentCreate→DocumentEdit(rename+body)→
DocumentCommentCreate→DocumentDelete.

## /:slug/search
Input-only, debounced **WorkspaceSearch** (no submit). **Target:** type a
unique issue title, assert the result row appears.

## /:slug/semantic-search
"Find similar issues" **SemanticIssueMatches**; "Index a batch now"
**EmbeddingsRefresh**; `EmbeddingIndexState` on mount. **Targets:**
EmbeddingsRefresh (safe), then a SemanticIssueMatches query returns.

## /:slug/analytics
"Date range" select **WorkspaceAnalytics**; error retry. No other controls
(charts/tables static). **Target:** change date range → analytics re-queries.
