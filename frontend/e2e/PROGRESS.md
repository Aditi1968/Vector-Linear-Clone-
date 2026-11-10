# E2E full-button-coverage — progress

Branch: `e2e/full-button-coverage`. Live status; newest state wins.

## Phase 1 — Setup ✅
- [x] 1. Create QA team (QA/QA) — id `01a1c1b3-…`, via `/onboarding/team`
- [x] 2. Hook staging into `playwright.config.ts` (staging-setup + staging) + `test:e2e:staging` script
- [x] 3. Add `e2e/helpers.ts` (`expectMutation`)
- [x] 4. GitHub issue #1 (re-login skips required team creation)

## Phase 2 — Inventory ✅
- [x] 5. `e2e/INVENTORY.md` built (6 clusters inventoried). Static pass: NO broken controls — every control named + wired; one intentional native `<details>` (NO-HANDLER by design). Safety rules recorded (unique data, isolated logout, GitHub-skip, members).

## Phase 3 — Tests (in progress)
Groups (6), ≤2 concurrent in worktrees, each writes `e2e/staging/<group>.spec.ts`:
- `admin` ✅ DONE — 7 pass / 1 skip (GitHub disconnected → WorkspaceGithubAutomationSet self-skips). No @bug, no dead controls. admin.spec.ts in main tree.
- `views` ✅ DONE — 8/8 green, no @bug, no dead controls. views.spec.ts in main tree.
- `issues` ✅ DONE — agent stalled on watchdog but spec was complete; I verified: 11 pass / 2 skip (triage: no UI files into the triage queue — intake is API/email only; mark-all-read: 0 unread). No @bug, no dead controls.
- `collaboration` — running
- `planning` — running (retry; first attempt stalled before writing a spec)
- queued: `shell`

Collected specs in main tree: admin, views, issues (committed). Stalled worktree agents (ade06cdf issues, a5fa2745 planning-v1) left in place for cleanup.

### Candidate findings (for Phase 4 FINDINGS.md)
- **Issue composer cold-start race** (from views agent): `useCreateIssue.ts` files into `teams[0]` from `IssueWorkspaceContext`; submitting before that query resolves returns "This workspace has no team to file issues in." and fires NO mutation, though the QA team exists (sidebar uses a different query). Degraded/cosmetic, cold-start only. Verify + triage in Phase 4.
- Inventory nit: shared `Menu` sets `label` as aria-label only when it has NO children; favorites header menu accessible name is "Add favorite" (not "Add a favorite").
Note: agent worktrees branch from main (577b868), NOT this branch — they copy staging infra (config, helpers, .env, .auth, INVENTORY) from the main checkout and symlink node_modules via Node (powershell/cmd blocked by the worktree hook). Specs collected via copy to main `e2e/staging/<group>.spec.ts`.
- [x] 6. All 6 groups done; specs collected + committed.
- [x] 7. Merged suite run; de-flaked (crowding + read-after-write under workers=2). No `@bug` app-broken tests.

## Phase 4 — Fix ✅
- [x] 8. `e2e/FINDINGS.md` written (coverage audit, assertion depth, determinism, the one bug + fix review).
- [x] 9. Fixed the composer cold-load bug (`51a124f`) with a regression test proven to fail pre-fix. No risky/auth/migration fixes needed.
- [x] 10. Final report delivered; stopped before push.

## Self-audit additions
- Coverage: wrote the 5 missing mutation tests (`199a9db`).
- Determinism: fixed IssueSetCycle/IssueSetProject crowding flakes + saved-view waits (`477cb84`, `3747cb4`); swept 28 accumulated saved views.
- det6 reaped by OS low-memory reaper; not relaunched (awaiting user).

Worktree recipe (proven): git worktree → PowerShell junction node_modules from main → copy e2e/.env → staging config resolves.

⚠️ CLEANUP SAFETY: a worktree's `frontend/node_modules` is a JUNCTION to the
main install. `git worktree remove --force` (and rm -rf) follow the junction
and wipe the REAL `frontend/node_modules`. Before removing ANY agent worktree:
first delete the junction link only — `cmd //c rmdir "<wt>\frontend\node_modules"`
(no /S) — THEN remove the worktree. (Already bit us once; restored via `npm ci`.)

## Phase 4 — Fix
- [ ] 8. `e2e/FINDINGS.md`: each failing/dead button, root cause, severity, repro test
- [ ] 9. Fix in severity order (one commit each); risky/auth/migration → write up, don't fix
- [ ] 10. Final rerun + report
