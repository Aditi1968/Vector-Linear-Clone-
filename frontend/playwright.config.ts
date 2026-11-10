import { defineConfig, devices } from '@playwright/test'

/**
 * The end-to-end suite: a real browser against the real deployment shape.
 *
 * Everything else in this repository tests a layer. `npm test` renders
 * components against a mocked Apollo link; `pytest -m db` drives services
 * against a real PostgreSQL with no browser above them. Neither can see the
 * seam this file exists for -- the session cookie travelling between a
 * browser and FastAPI through nginx, on one origin -- and that seam is where
 * authentication and tenant isolation actually live.
 *
 * The stack under test is `docker compose up`: PostgreSQL 18 with pgvector,
 * every migration through the repository's own runner, FastAPI, and nginx
 * serving the built bundle with /graphql proxied same-origin. It is the
 * production build, not the dev server, which is the point -- a suite that
 * passed against `vite dev` would be testing a proxy no deployment has.
 *
 * ## No `webServer`
 *
 * Playwright can start a server for you. It is not used, because bringing
 * this stack up is `docker compose up --build --wait` -- a build, a database,
 * a migration job and two health gates -- and burying that in a config option
 * would mean the local command and the CI step were different things that
 * only look the same. CI runs compose as its own step (see the `e2e` job in
 * .github/workflows/ci.yml) and this config assumes the result is already
 * answering.
 */

/**
 * Where the stack is answering.
 *
 * Defaults to the port docker-compose.yml publishes, so the common case needs
 * no environment at all. Overridable because docker-compose.yml itself takes
 * `VECTOR_WEB_PORT` -- a developer already running `run-vector-local.ps1` on
 * 5173 brings the compose stack up somewhere else, and the suite has to be
 * able to follow it there.
 *
 * 127.0.0.1 rather than localhost: on Windows, localhost resolves to ::1
 * first, and compose publishes on the IPv4 loopback only.
 */
const baseURL = process.env.VECTOR_E2E_BASE_URL ?? 'http://127.0.0.1:5173'

/*
  Staging is a separate deployment, driven by a separate pair of projects
  (`staging-setup` + `staging`) that live beside the local `chromium` project
  below without touching it. Credentials and base URL come from e2e/.env,
  loaded here so both this config and the sign-in setup see them. The file is
  gitignored and absent in CI's local-stack run, so a missing file is not an
  error -- only the staging projects need it, and they fail loudly if asked to
  run without it.
*/
try {
  process.loadEnvFile('./e2e/.env')
} catch {
  // No e2e/.env: local runs do not need one.
}
const stagingBaseURL = process.env.E2E_BASE_URL

export default defineConfig({
  testDir: './e2e',

  // The staging projects keep their specs in ./e2e/staging; everything else
  // (the local `chromium` project) ignores that folder, so the two suites
  // never pick up each other's files. The staging projects clear this with
  // `testIgnore: []` so they can see their own.
  testIgnore: '**/staging/**',

  /*
    One worker, and it is not a performance compromise.

    Every test here signs up a brand new account and creates its own
    workspace, so the tests do not contend for data and could in principle
    run in parallel. What they would contend for is memory: each worker is a
    Chromium, and this suite runs beside a PostgreSQL, a uvicorn and an nginx
    on developer laptops where free memory is measured in hundreds of
    megabytes. A browser the OS starts swapping produces timeouts that read
    exactly like product bugs, and chasing one of those costs more than the
    minutes saved.
  */
  workers: 1,
  fullyParallel: false,

  /*
    Zero retries, deliberately, and this is the load-bearing decision in the
    file.

    A retry does not fix a flaky test; it hides one, and a suite that hides
    its own flakiness is disabled within a month. The tests below use
    web-first assertions and `waitForResponse` and contain no sleep, so a
    failure here is a real failure -- either the product broke or the suite
    is wrong about the product, and both are worth stopping for.
  */
  retries: 0,

  /*
    Sixty seconds, against a default of thirty. A single test signs up,
    walks four onboarding steps and creates an issue, each step a real round
    trip to PostgreSQL, and the first test of a run pays for a cold uvicorn
    on top. The per-assertion timeout below is what actually catches a hung
    product; this one only has to be larger than an honest journey.
  */
  timeout: 60_000,
  expect: { timeout: 10_000 },

  // A `.only` left in a spec silently narrows the suite to one test and
  // reports green. Locally that is a convenience; in CI it is a gate that
  // stopped running.
  forbidOnly: !!process.env.CI,

  reporter: [
    ['list'],
    // Written, never opened. `open: 'never'` matters in CI, where the
    // default would try to serve the report and hold the job open until it
    // is cancelled -- reported as a timeout rather than as a test result.
    ['html', { open: 'never' }],
  ],

  use: {
    baseURL,

    /*
      Kept only for a test that failed, which is the only run whose trace
      anybody reads. `on-first-retry` -- the usual choice -- would never
      produce anything at all here, because `retries` is 0.
    */
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',

    // The stack is plain http on loopback and its session cookie is
    // therefore not Secure (see app/http_cookies.py). Nothing here needs to
    // ignore a certificate, and a suite that ignored them would be unable to
    // notice the day one matters.
  },

  projects: [
    {
      // Chromium alone. The three engines cost roughly 400 MB to install and
      // three times the wall clock, to answer a question this suite is not
      // asking: nothing below tests rendering or a vendor-specific API. It
      // tests cookies, tenant isolation and the GraphQL round trip, which are
      // the same in every engine. A cross-browser matrix is a decision to
      // make when there is a rendering bug to catch with it.
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },

    /*
      Staging. Two projects in the standard setup-dependency shape: `staging`
      depends on `staging-setup`, which signs in once and saves the session to
      e2e/.auth/user.json, so every staging spec starts already authenticated
      against the shared e2e-playground workspace.

      These run only when selected (`--project=staging`); a bare
      `playwright test` still runs the local `chromium` project unchanged.
      Worker count is set on the `test:e2e:staging` npm script (Playwright has
      no per-project `workers`), so the local project keeps `workers: 1` above.
    */
    {
      name: 'staging-setup',
      testDir: './e2e/staging',
      testIgnore: [],
      testMatch: /.*\.setup\.ts/,
      // Provisions a fresh workspace; its paired teardown archives it once the
      // staging project (which depends on this one) has finished.
      teardown: 'staging-teardown',
      use: { ...devices['Desktop Chrome'], baseURL: stagingBaseURL },
    },
    {
      name: 'staging-teardown',
      testDir: './e2e/staging',
      testIgnore: [],
      testMatch: /.*\.teardown\.ts/,
      use: { ...devices['Desktop Chrome'], baseURL: stagingBaseURL },
    },
    {
      name: 'staging',
      testDir: './e2e/staging',
      testIgnore: [],
      testMatch: /.*\.spec\.ts/,
      dependencies: ['staging-setup'],
      timeout: 60_000,
      use: {
        ...devices['Desktop Chrome'],
        baseURL: stagingBaseURL,
        storageState: 'e2e/.auth/user.json',
      },
    },
  ],
})
