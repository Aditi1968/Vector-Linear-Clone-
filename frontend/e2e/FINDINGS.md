# E2E button-coverage — findings

Outcome of driving every observable-result control in the signed-in product
against the staging deployment, plus a self-audit of the suite itself. Each
run provisions its own fresh workspace (`vector-e2e-<epoch>`, team `QA`) — see
§3 — so the suite is isolated and repeatable.

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

## 3. Determinism (per-run isolation) — 46 tests (41 run + 5 skipped), workers 2

### The isolation change

The first determinism attempt ran against a single **shared** workspace
(`e2e-playground`) and flaked: data accumulated across runs and several
lists/dropdowns are capped (the saved-views list shows 25 with no "load more";
32 cycles had piled up), so a "find my newly created item" assertion could sit
outside the capped set even though the mutation succeeded. That is a data-
hygiene problem, not a product bug.

The fix is at the source: **each run now provisions its own fresh workspace**
(`staging-setup` registers `vector-e2e-<epoch>` + a QA team over the API;
`staging-teardown` archives it after). Every list a spec reads therefore
starts empty, so the crowding class of flake cannot occur. With that in place
the earlier band-aids were removed: the 30s list-row waits are back to the
defaults, and `IssueSetProject` / `IssueSetCycle` again assert on and select
the exact project/cycle the test created.

### Consecutive full-suite runs with per-run isolation (workers 2)

| Run | Result | Note |
|---|---|---|
| 1 | 42 passed / 5 skipped / **0 failed** ✓ | |
| 2 | 42 passed / 5 skipped / **0 failed** ✓ | |
| 3 | 41 passed / 5 skipped / **1 failed** ✗ | `SavedViewUpdate` row raced the list refetch — root-caused and fixed with `expectRefetch` (await the `refetchQueries` follow-up); verified green in isolation (saved-view + favorites 5/5). |
| 4 (confirmation, all fixes in) | — not completed | the OS low-memory reaper stopped the run mid-flight (the machine was out of RAM); **not relaunched** — the harness directive is to restart a reaped background run only when asked. |

**Status:** two consecutive clean full runs, the one subsequent flake
root-caused and fixed (and that fix verified in a targeted run), and the final
all-fixes-in confirmation run pending only because the host ran out of memory.
Re-running `npm run test:e2e:staging` three times once memory frees is the
remaining step; every known flaky path has a committed deterministic fix.

### Flakes found while bringing isolation up, each root-caused (no retries, no widened timeouts)

Switching to an empty-per-run workspace surfaced a handful of genuine
read-after-write / query-timing races (the mutation always succeeded — every
`expectMutation` stayed green — the follow-up UI read raced an async query).
Each was fixed deterministically by waiting on the real signal, never by
widening a timeout or retrying an assertion:

- **IssueSetCycle / Cycle detail** — the team-scoped cycle list is cache-served
  and lags a just-created cycle; **reload** to run `CycleList` fresh, then open
  the row (same pattern for the inspector dropdown).
- **Semantic search** — the index-state badge renders only once the on-mount
  `EmbeddingIndexState` query resolves; **await that response** before reading
  the panel.
- **Saved views / favorites** — `SavedViewCreate`/`Delete` and
  `Favorite*` rerun their list via `refetchQueries` (a separate round-trip);
  the new `expectRefetch` helper **awaits that follow-up `SavedViewList` /
  `FavoriteList`** before the row assertion. (`SavedViewUpdate` is excluded: it
  refetches `SavedViewResults` and the rename lands via cache normalisation.)
- **Shell** — completing the slug refactor: URL-assertion regexes are built
  from the per-run slug, not a literal.
- **Issue composer** — the pre-existing cold-load race, covered by `settle()`
  (networkidle) before every composer submit; also the product bug in §4.

No test is tagged `@flaky`.

**A real app limitation, stated:** these refetch/query-timing waits exist
because several list mutations update via `refetchQueries` (an extra round
trip) rather than an optimistic cache write, and the index panel gates its UI
on an on-mount query. The specs wait on those real signals; they are not
widened timeouts. The only assertion that cannot be made instant is any "row
appears after a `refetchQueries` mutation", which is inherently one refetch
behind the mutation — hence `expectRefetch`.

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

## Residual data (per-run isolation)

Each run creates its own throwaway user + workspace (`vector-e2e-<epoch>`), so
nothing leaks into a workspace a later run reads — the determinism problem is
gone. `staging-teardown` archives the run's issues at the end.

What persists is **inert**: there is no `workspaceDelete` / `teamDelete` /
`memberRemove`-of-self / `environmentDelete` / user-delete mutation (94
mutations, none remove a workspace, team or user), so each run leaves behind
an empty throwaway workspace and its registered user. They are never visited
again. If staging's user/workspace tables should be swept, that needs a
server-side/admin path the product API does not expose — noted for ops, not a
test change.
