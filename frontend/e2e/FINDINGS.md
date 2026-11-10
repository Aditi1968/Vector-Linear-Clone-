# E2E button-coverage — findings

Outcome of driving every observable-result control in the signed-in product
against the staging deployment (`e2e-playground`, team `QA`), plus a self-audit
of the suite itself.

## Headline

- **No failing buttons and no dead buttons.** Every wired control that can be
  reached and observed on staging fires its GraphQL operation and returns a
  200 with no `errors`. The static inventory pass already found no unnamed or
  unwired controls; the runtime pass found no control that looked like it
  worked but didn't.
- **One latent product bug, found and fixed:** the issue composer could submit
  before its team lookup resolved and fail with a false "no team" error
  (details below). Fixed in `51a124f` with a regression test.
- The specs live under `frontend/e2e/staging/`, one per route cluster, and run
  as the `staging` Playwright project (`npm run test:e2e:staging`, workers 2).

---

## 1. Coverage audit (every INVENTORY mutation row → covered / reason)

46 tests across 7 files (incl. setup + smoke). Every mutation op in
`INVENTORY.md` is classified below.

### Covered by a dedicated test (observable GraphQL op asserted)
`IssueCreate`, `IssueUpdate`, `IssueSetProject`, `IssueSetCycle`,
`IssueArchive`, `BoardIssueMove`, `CommentCreate`, `CommentDelete`,
`LabelCreate`, `IssueLabelAttach`, `IssueLabelDetach`, `IssueRelationCreate`,
`IssueRelationDelete`, `IssueSetParent`, `IssueClearParent`, `ProjectCreate`,
`ProjectUpdate`, `ProjectDelete`, `ProjectTeamAdd`, `ProjectTeamRemove`,
`ProjectMilestoneCreate`, `ProjectMilestoneUpdate`, `ProjectMilestoneDelete`,
`CycleCreate`, `CycleUpdate`, `CycleDelete`, `InitiativeCreate`,
`InitiativeUpdate`, `InitiativeDelete`, `InitiativeProjectAdd`,
`InitiativeProjectRemove`, `InitiativeSetParent`, `InitiativeClearParent`,
`InitiativeUpdatePost`, `MemberInvitationCreate`, `MemberInvitationRevoke`,
`EnvironmentCreate`, `LabelGroupCreate`, `LabelGroupDelete`,
**`LabelGroupUpdate`**, **`LabelSetGroup`**, `IssueTemplateCreate`,
`IssueTemplateDelete`, `IssueCreateFromTemplate`, **`IssueTemplateUpdate`**,
`SavedViewCreate`, `SavedViewUpdate`, `SavedViewDelete`, `FavoriteAdd`,
`FavoriteRemove`, `FavoriteReorder`, `DocumentCreate`, `DocumentEdit`,
`DocumentDelete`, `DocumentCommentCreate`, **`DocumentCommentDelete`**,
**`DocumentRestore`**, `EmbeddingsRefresh`, `Logout`. Plus the queries
`CommandSearch`, `WorkspaceSearch`, `SemanticIssueMatches`,
`WorkspaceAnalytics`, `IssueSearch`, `IssueList`, `BoardIssues`,
`NotificationInbox`.

The **bold five** were exercised by no test and skipped with no stated reason
in the first pass; the self-audit caught them and added tests (`199a9db`).

### Not separately tested — same op already proven (field/variant of a covered op)
| Control(s) | Op | Why not separate |
|---|---|---|
| Inspector Priority / Assignee / Estimate / Due date | `IssueUpdate` | same op proven via Status + Title |
| Board Assignee/Label/Priority/Project/Cycle/Team filters, Clear | `BoardIssues` | same op proven via Status filter + Sort |
| Project screen "Add issue" / "Move to milestone" / "Remove from project" | `IssueSetProject` | same op proven in the inspector |
| Sub-issues "Set parent" (direct) | `IssueSetParent` | same op proven via "Add" child |

### Not tested — no backend op (NAV / client-only / clipboard)
Sidebar nav links (18; one representative asserts URL + `aria-current`),
command-palette "Go to" entries + "Keyboard shortcuts", "Workspace settings",
"Close issue", team disclosure + team sub-links, row-density / group-by radios,
collapse/expand sidebar, roadmap (view-only, asserted it renders), My Issues
(list/pagination only), "Copy invite link" (clipboard).

### Not tested — reachable state can't be created on this staging (reason)
| Control(s) | Op | Reason |
|---|---|---|
| Triage Accept / Decline / MarkDuplicate / ChangeTeam / set-priority / assign | `TriageAccept` etc. | **No product UI files an issue into the triage queue** — the composer always assigns a state; triage intake is API/email only. Nothing to accept. |
| Releases Cut / set status / delete | `ReleaseCreate` etc. | The "Cut a release" form requires a connected GitHub repository; staging has none, so the form shows a block instead of fields. |
| Settings PR-automation toggle + repo tracking | `WorkspaceGithubAutomationSet`, `WorkspaceGithubRepositoriesSet`, disconnects | GitHub/Slack are disconnected on staging; the controls don't render. The test self-skips on the visible "Connect GitHub" link. |
| Members "Make {role}" / "Remove from workspace" | `MemberRoleUpdate`, `MemberRemove` | Single-member workspace; demoting/removing the only owner would lock the shared account out. The owner-menu test asserts "Owner (current)" is disabled (observable, safe). |
| Inbox "Mark read" (per row) | `NotificationMarkRead` | Single-member workspace has no unread notifications to mark. "Mark all read" self-skips when 0 unread. |
| Switch workspace | — | Only renders with ≥2 workspaces; the test account has one. |
| "Load more comments" / doc history pagination (`fetchMore`) | queries | Would need dozens of comments/versions on one record to paginate; not worth the write volume on the small instance. |

**Net: every reachable mutation with an observable result is covered. The
only uncovered mutations are blocked by a missing precondition on staging
(GitHub, a second member/team/workspace, a triage intake path), each noted
above and each written as a guarded `test.skip` where the screen is reachable.**

---

## 2. Assertion depth

**Every test whose control fires a mutation asserts the GraphQL response via
`expectMutation(page, 'Op')`** (helpers.ts): it waits for the POST, asserts
HTTP 200 and no top-level `errors`, so a button whose mutation came back with
errors fails with the op name — not merely a DOM check. No DOM-only test
exists for a mutation-firing control.

Query-driven controls (search inputs, the analytics date range, the issue
picker) assert on the rendered result, which is the correct observable for a
read; `WorkspaceAnalytics` additionally asserts the response status 200.

---

## 3. Determinism (3 full-suite runs, workers 2)

Suite: 46 tests (42 run + 4 skipped), `--workers=2`. Baselines on the earlier
41-test suite: 37/4/0 twice (identical).

Three runs of the final 46-test suite, then the fixes, then re-verification:

| Run | Result | Note |
|---|---|---|
| 1 | 42 passed / 4 skipped / **0 failed** ✓ | |
| 2 | 41 passed / 4 skipped / **1 failed** ✗ | `IssueSetCycle` — cycle not found in a crowded dropdown |
| 3 | 42 passed / 4 skipped / **0 failed** ✓ | |
| (during fixing) | 40 / 4 / **2 failed** ✗ | `IssueSetCycle` + `FavoriteReorder` — surfaced the root cause |
| 5 (post-fix) | 42 passed / 4 skipped / **0 failed** ✓ | all fixes in |
| 6 (post-fix) | — | stopped by the OS low-memory reaper mid-run; not a test failure, not relaunched |

Plus targeted re-verification at workers 2 after the fixes: the previously
flaky `SavedView*` + `Favorite*` tests 4/4 ✓, and `IssueSetCycle` ✓.

**Root cause of the flakes (one class):** the shared `e2e-playground`
workspace accumulates test data across runs, and several lists/dropdowns are
capped (e.g. the saved-views list shows 25 with no "load more"; 32 cycles had
piled up). Once a list filled, a "find my newly created item" assertion could
never pass — the item was created (every `expectMutation` stayed green) but
sat outside the capped/ordered set. Read-after-write refetch lag under
workers 2 widened the window. **This is an environment/data-hygiene issue, not
a product bug and not pure timing.**

**Fixes (root cause, not retries):**
1. `IssueSetProject` / `IssueSetCycle` now select whatever real option the
   inspector dropdown offers (index 1; index 0 is the placeholder) and assert
   the field ends non-empty — still driving the mutation, but immune to a
   crowded dropdown instead of hunting for this run's specific item.
2. Saved-view / favorite "row appears" waits raised to 30s for the list
   refetch under load.
3. `DocumentCommentDelete` edits the body first and reads the comment back via
   the settled `Comments 1` count tab (a body-less doc's comment tab updated
   unreliably under load) — mirrors the proven `DocumentEdit` comment path.
4. One-time data sweep: deleted 28 accumulated `e2e …` saved views to bring
   the list back under its cap.

No test is tagged `@flaky`; nothing was retried into passing.

**Standing recommendation (environment):** to keep the suite green long-term,
either reset/clean the `e2e-playground` workspace periodically (the suite
cleans up its own data on success, but create-only APIs, no-delete-UI items,
and interrupted runs leave residue that re-crowds the capped lists), or point
the `staging` project at a workspace that is reset per run.

---

## 4. The one bug — issue composer cold-load race (FIXED)

**Severity:** degraded (not blocking). Only on a cold load / fast submit; when
the workspace context is already cached (the common case) the button is enabled
immediately.

**Symptom:** opening the issue composer and submitting before the
`IssueWorkspaceContext` query answered produced the error *"This workspace has
no team to file issues in."* and fired **no** mutation — a false claim about a
workspace that has a team (`QA`).

**Root cause:** `useCreateIssue` files into the first team from
`IssueWorkspaceContext`, but the composer's submit button was disabled only
while `isSubmitting`, never while that lookup was in flight
(`IssueComposer.tsx`). The hook's own comment said the no-team case should be
"a disabled state rather than a rejected submission," but the hook exposed no
readiness signal for the composer to gate on.

**Fix (`51a124f`):** `useCreateIssue` now surfaces the real
`IssueWorkspaceContext` loading state as `isPreparing`, and the composer
disables Create on `isSubmitting || isPreparing`. A workspace that genuinely
has no team still gets the `NO_TEAM` outcome (that case is false once the
lookup answers). This gates on the actual loading state — **not** a timeout or
a retry.

**Regression test:** `create.test.tsx` →
*"keeps Create disabled until the team lookup answers, so a premature submit
cannot fail with 'no team'"*. It renders the list, opens the composer, leaves
`IssueWorkspaceContext` unresolved, and asserts Create is disabled; then
resolves the lookup and asserts it arms. **Confirmed it fails on the pre-fix
code** (button armed while loading → `toBeDisabled()` fails) by stashing only
the source fix and running the test, then restoring. The a11y tab-order test
was updated to resolve the context (its ready state) so Create is focusable.

The staging specs encode the same race defensively with a `settle()`
(networkidle) wait before every composer submit.

---

## Deferred / not fixed (and why)

Nothing was deferred for risk. The uncovered controls in §1 are environment
limits on staging (no GitHub install, single member/team/workspace, no triage
intake path), not product defects — no code change is warranted, so they are
documented as guarded skips rather than fixed. No change touched auth, tenant
isolation, or migrations.

---

## Residual test data (all in `e2e-playground`, all uniquely `e2e <ts>`-stamped)
Create-only or no-delete-UI records accumulate per run: environments
(create-only API), workspace labels minted for `LabelSetGroup` (labels have no
delete UI), `QA-N` issues from `IssueCreateFromTemplate` and from search/board
tests, and a handful of documents from earlier failed iterations. None collide
across runs. A periodic manual sweep of the `e2e …` rows is the cleanup.
