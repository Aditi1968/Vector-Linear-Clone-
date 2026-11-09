# E2E full-button-coverage — progress

Branch: `e2e/full-button-coverage`. Live status; newest state wins.

## Phase 1 — Setup ✅
- [x] 1. Create QA team (QA/QA) — id `01a1c1b3-…`, via `/onboarding/team`
- [x] 2. Hook staging into `playwright.config.ts` (staging-setup + staging) + `test:e2e:staging` script
- [x] 3. Add `e2e/helpers.ts` (`expectMutation`)
- [x] 4. GitHub issue #1 (re-login skips required team creation)

## Phase 2 — Inventory ✅
- [x] 5. `e2e/INVENTORY.md` built (6 clusters inventoried). Static pass: NO broken controls — every control named + wired; one intentional native `<details>` (NO-HANDLER by design). Safety rules recorded (unique data, isolated logout, GitHub-skip, members).

## Phase 3 — Tests
- [ ] 6. Per-route subagents (≤2 concurrent, own worktrees) write `e2e/<group>.spec.ts`
- [ ] 7. Merge, run `npm run test:e2e:staging`, de-flake; app bugs tagged `@bug`

## Phase 4 — Fix
- [ ] 8. `e2e/FINDINGS.md`: each failing/dead button, root cause, severity, repro test
- [ ] 9. Fix in severity order (one commit each); risky/auth/migration → write up, don't fix
- [ ] 10. Final rerun + report
