# E2E default decisions

Small, reversible choices made while building the staging button-coverage
suite, recorded so they are visible rather than buried in scripts.

| Decision | Value | Why |
|---|---|---|
| Staging base URL | `https://vector-staging-qd79.onrender.com` | from `e2e/.env` (`E2E_BASE_URL`) |
| Test workspace | `e2e-playground` (slug `e2e-playground`) | the one sandbox we are allowed to touch |
| Tester account | `E2E_EMAIL` in `e2e/.env`, display name `E2E Tester` | created in an earlier step |
| First team | name `QA`, key `QA` | given |
| "Normal team UI" | `/onboarding/team`, reached by direct navigation | no in-product team-creation UI exists; `features/onboarding/lib/progress.ts` documents this URL as a legitimate action for an existing user |
| Test-data naming | every test mints its own with prefix `e2e <ISO-timestamp>` | tests never share or depend on each other's data |
| Driving scripts | kept in the session scratchpad, **not** committed | one-off probes; only spec files, helpers, config and docs land in the repo |
| Staging workers | 2 | small Render instance; more would contend |
| Commit attribution | none (no Co-Authored-By / AI trailer) | per repo memory `no-ai-coauthor-trailers` |
