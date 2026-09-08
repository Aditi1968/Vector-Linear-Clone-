import { randomUUID } from 'node:crypto'

import { expect, test } from '@playwright/test'
import type { APIRequestContext, Page } from '@playwright/test'

import { paths, publicPaths } from '../src/app/routes/paths'

/**
 * Four journeys through a real browser, against the compose stack.
 *
 * Depth over breadth, deliberately. There is no test per screen here and
 * there should not be: a suite with thirty shallow smoke tests takes thirty
 * times as long to run and catches the same one bug -- "the page rendered" --
 * thirty times. What is below instead is the four properties nothing else in
 * this repository can check, because each of them only exists once a browser,
 * nginx, FastAPI and PostgreSQL are all in the same sentence:
 *
 *   1. a session survives sign-up, sign-out and sign-in through nginx, and
 *      the cookie carrying it has the attributes app/http_cookies.py intends;
 *   2. an issue created in the UI reaches PostgreSQL and comes back on a
 *      reload, not merely into an Apollo cache;
 *   3. a second account cannot see the first's workspace or its issues,
 *      through the UI *or* by asking the API directly;
 *   4. a signed-out visitor on a protected URL gets a safe refusal rather
 *      than a crash or somebody's data.
 *
 * ## Rules this file keeps
 *
 * No `waitForTimeout`, anywhere, ever. Every wait is a web-first assertion or
 * a `waitForResponse`. A sleep is a guess about how fast a machine is, and a
 * suite built on guesses is disabled within a month of the first CI runner
 * that is slower than a laptop -- at which point it is worse than nothing,
 * because the repository still looks like it has end-to-end coverage.
 *
 * Every locator is a role and an accessible name. Not a CSS class, not a test
 * id. That is not purity: it means this suite also fails the day a button
 * loses its label or a field loses its `<label for>`, so accessibility
 * regressions are caught by the tests that were going to run anyway.
 *
 * Every test creates its own account, its own workspace and its own issues,
 * with a unique address per run. Nothing here depends on a seeded database,
 * on another test having run first, or on being run in a particular order --
 * so a single test can be run alone to debug it, which is the moment a
 * shared-fixture suite always lets you down.
 */

/**
 * The session cookie's name and policy, restated from app/http_cookies.py.
 *
 * Restated rather than imported because that file is Python and this one is
 * TypeScript. The duplication is the point of the test that reads it: these
 * are what the server *intends*, asserted against what a real Chromium
 * actually stored after a real sign-in through nginx. If the two drift, the
 * cookie test below fails and names the attribute.
 */
const SESSION_COOKIE_NAME = 'vector_session'
const SESSION_COOKIE_PATH = '/'
const SESSION_COOKIE_SAMESITE = 'Lax'

/** The team every account creates. Its key is the ENG in ENG-1. */
const TEAM_NAME = 'Engineering'
const TEAM_KEY = 'ENG'

interface Account {
  readonly email: string
  readonly password: string
  readonly name: string
  readonly workspaceName: string
  readonly workspaceSlug: string
}

/**
 * A fresh identity, unique to this run.
 *
 * The email and the workspace slug are both globally unique in the schema, so
 * a fixed value would pass once and then fail with EMAIL_TAKEN and SLUG_TAKEN
 * for the rest of the volume's life. A random suffix means the suite can be
 * run repeatedly against a stack that is never reset -- which is what
 * `docker compose down` without `-v` leaves behind, and what a developer
 * running this twice in a row actually has.
 */
function newAccount(): Account {
  const id = randomUUID().replaceAll('-', '').slice(0, 10)

  return {
    email: `e2e-${id}@example.com`,
    // Over app.services.auth.PASSWORD_MIN_LENGTH, and the same dictionary-
    // words placeholder src/features/auth/auth.test.tsx already uses. It
    // guards an account that exists for one test run against a database on
    // loopback.
    password: 'correct-horse',
    name: `Ada ${id}`,
    workspaceName: `E2E ${id}`,
    workspaceSlug: `e2e-${id}`,
  }
}

/** A title no other run could have produced, so an assertion on it is exact. */
function newIssueTitle(): string {
  return `E2E issue ${randomUUID().slice(0, 8)}`
}

/**
 * Register, then walk the whole of onboarding, and end up in the workspace.
 *
 * Clicked through rather than short-cut with `page.goto`, even though the
 * final URL is knowable. Registering does not navigate anywhere on its own --
 * `RequireNoAuth` recomputes the destination from a `myWorkspaces` refetch --
 * and neither does creating the workspace, so this path is also the assertion
 * that those two refetch-driven transitions still happen. A `goto` past them
 * would test the router and nothing else.
 */
async function signUp(page: Page): Promise<Account> {
  const account = newAccount()

  await page.goto(publicPaths.register())
  await expect(page.getByRole('heading', { name: 'Create your Vector account' })).toBeVisible()

  await page.getByLabel('Name', { exact: true }).fill(account.name)
  await page.getByLabel('Email', { exact: true }).fill(account.email)
  await page.getByLabel('Password', { exact: true }).fill(account.password)
  await page.getByRole('button', { name: 'Create account' }).click()

  // A brand new account has no workspace, so the destination is onboarding.
  await expect(page.getByRole('heading', { name: 'Create your workspace' })).toBeVisible()

  await page.getByLabel('Workspace name', { exact: true }).fill(account.workspaceName)
  // Filled by hand rather than left to derive from the name. The derivation
  // is a convenience this suite is not testing, and depending on it would
  // make every assertion below depend on a slugifier.
  await page.getByLabel('Workspace URL', { exact: true }).fill(account.workspaceSlug)
  await page.getByRole('button', { name: 'Create workspace' }).click()

  await expect(page.getByRole('heading', { name: 'Create your first team' })).toBeVisible()

  await page.getByLabel('Team name', { exact: true }).fill(TEAM_NAME)
  await page.getByLabel('Issue key', { exact: true }).fill(TEAM_KEY)
  await page.getByRole('button', { name: 'Create team' }).click()

  // Invitations and integrations are both skippable, and both are skipped:
  // one needs a second person and the other needs GitHub. Clicking through
  // them still proves each step renders and each link resolves.
  await page.getByRole('link', { name: 'Skip for now' }).click()
  await page.getByRole('link', { name: /^Finish and open/ }).click()

  await expect(page).toHaveURL(paths.issues(account.workspaceSlug))

  return account
}

/** Sign in an account that already exists. Does not assert where it lands. */
async function signIn(page: Page, account: Account): Promise<void> {
  await page.goto(publicPaths.login())
  await expect(page.getByRole('heading', { name: 'Sign in to Vector' })).toBeVisible()

  await page.getByLabel('Email', { exact: true }).fill(account.email)
  await page.getByLabel('Password', { exact: true }).fill(account.password)
  await page.getByRole('button', { name: 'Sign in' }).click()
}

/**
 * File an issue through the composer and return the id the server gave it.
 *
 * The id comes out of the mutation's own response rather than out of the URL
 * after clicking the new row, because the tenancy test needs an id it can
 * hand to the API as somebody else -- and reading it from the wire is also
 * how this function asserts the mutation succeeded rather than returning a
 * `null` payload full of validation errors.
 */
async function createIssue(page: Page, title: string): Promise<string> {
  await page.getByRole('button', { name: 'New issue' }).click()
  await expect(page.getByRole('heading', { name: 'New issue' })).toBeVisible()

  await page.getByRole('textbox', { name: 'Title' }).fill(title)

  // Waiting on the response rather than on the row appearing, and both are
  // asserted: the row could appear from an optimistic cache write, so the
  // response is what proves a round trip happened at all.
  const created = page.waitForResponse(
    (response) =>
      response.url().endsWith('/graphql') &&
      response.request().method() === 'POST' &&
      (response.request().postData() ?? '').includes('IssueCreate'),
  )

  await page.getByRole('button', { name: 'Create issue' }).click()

  const body: unknown = await (await created).json()
  const payload = body as {
    errors?: unknown[]
    data?: { issueCreate?: { issue?: { id?: string } | null; errors?: unknown[] } }
  }

  expect(payload.errors, 'IssueCreate returned a top-level GraphQL error').toBeUndefined()
  expect(payload.data?.issueCreate?.errors, 'IssueCreate was rejected').toEqual([])

  const id = payload.data?.issueCreate?.issue?.id
  expect(id, 'IssueCreate returned no issue').toBeTruthy()

  return id as string
}

interface GraphqlResult {
  readonly status: number
  readonly text: string
  readonly body: {
    data?: Record<string, unknown> | null
    errors?: { message?: string; extensions?: { code?: string } }[]
  }
}

/**
 * Ask the API directly, over the same origin and with the caller's cookies.
 *
 * `APIRequestContext` taken from a browser context shares that context's
 * cookie jar, so this is the same caller the page is -- which is what makes
 * it useful for the two questions the UI cannot answer on its own: what the
 * server says to a member of another workspace, and what it says to nobody.
 * A screen that renders nothing is not proof that nothing was sent.
 *
 * The raw text is returned alongside the parsed body so a test can assert
 * that a title appears nowhere in the response at all, rather than only that
 * the field it happened to look at was null.
 */
async function graphql(
  request: APIRequestContext,
  query: string,
  variables: Record<string, unknown>,
): Promise<GraphqlResult> {
  const response = await request.post('/graphql', { data: { query, variables } })
  const text = await response.text()

  return { status: response.status(), text, body: JSON.parse(text) as GraphqlResult['body'] }
}

const ISSUE_QUERY = `
  query E2EIssue($slug: String!, $id: UUID!) {
    issue(workspaceSlug: $slug, id: $id) { id title }
  }
`

test.describe('Vector, end to end', () => {
  test('a session survives sign-up, sign-out and sign-in through nginx', async ({ page }) => {
    /*
      What this catches that nothing else does: the cookie round trip. Every
      auth test in `npm test` mocks the transport, and every auth test in
      pytest asserts the header the application produced. Neither can tell
      you whether a real browser accepted that header, kept it across a
      navigation, sent it back through nginx's proxy, and stopped sending it
      after a sign-out -- which is the entire mechanism.
    */
    const account = await signUp(page)

    const sessionCookie = (await page.context().cookies()).find(
      (cookie) => cookie.name === SESSION_COOKIE_NAME,
    )

    expect(sessionCookie, `no ${SESSION_COOKIE_NAME} cookie after signing up`).toBeDefined()

    // Asserted against what app/http_cookies.py intends, not against what
    // happened to be sent. HttpOnly is the whole reason the token is in a
    // cookie rather than in a header the client stores; SameSite=Lax is what
    // pays for that choice by withholding it from cross-site POSTs; the path
    // is `/` because sign-out has to clear the cookie sign-in wrote, and a
    // mismatched path leaves the browser holding a revoked token.
    expect(sessionCookie?.httpOnly).toBe(true)
    expect(sessionCookie?.sameSite).toBe(SESSION_COOKIE_SAMESITE)
    expect(sessionCookie?.path).toBe(SESSION_COOKIE_PATH)

    // `is_secure_environment` marks the cookie Secure in production only, and
    // production is the only deployment served over https. Expressed as a
    // function of the scheme the suite is actually talking to, because a
    // Secure cookie on plain http is one the browser silently refuses to
    // store -- so getting this wrong looks like "authentication is broken",
    // not like "a flag is set too eagerly".
    expect(sessionCookie?.secure).toBe(new URL(page.url()).protocol === 'https:')

    // The other half of HttpOnly, and the half a header assertion cannot
    // make: the token is not readable from the document. This is what a
    // cross-site scripting bug would walk away with if the flag were wrong.
    const readableCookies = await page.evaluate(() => document.cookie)
    expect(readableCookies).not.toContain(SESSION_COOKIE_NAME)

    await page.getByRole('button', { name: 'Account' }).click()
    await page.getByRole('menuitem', { name: 'Sign out' }).click()

    await expect(page).toHaveURL(publicPaths.landing())
    expect(
      (await page.context().cookies()).map((cookie) => cookie.name),
      'the session cookie outlived sign-out',
    ).not.toContain(SESSION_COOKIE_NAME)

    // Signing in again lands in the workspace rather than in onboarding,
    // which is `RequireNoAuth` resolving a destination from `myWorkspaces` --
    // the same code path that sent a brand new account the other way.
    await signIn(page, account)
    await expect(page).toHaveURL(paths.issues(account.workspaceSlug))
  })

  test('an issue created in the UI is in PostgreSQL, not just in the cache', async ({ page }) => {
    /*
      What this catches: the whole product spine, in the order a user meets
      it. A mutation over GraphQL, a row in PostgreSQL, a refetch, a render.
      The reload in the middle is the part that matters -- without it this
      would pass against an Apollo cache write and a server that never
      answered.
    */
    const account = await signUp(page)

    // A new workspace is empty, and says so. Asserted before creating
    // anything: an empty state that never appears is how a list that is
    // silently failing to load looks.
    await expect(page.getByRole('button', { name: 'Create the first issue' })).toBeVisible()

    const title = newIssueTitle()
    const issueId = await createIssue(page, title)

    const row = page.getByRole('link', { name: new RegExp(title) })
    await expect(row).toBeVisible()

    await page.reload()
    await expect(row, 'the issue did not survive a reload').toBeVisible()

    // The identifier the team key produces. Rendered inside the row's link,
    // so this also pins that a workspace's first issue is numbered from 1.
    await expect(page.getByRole('link', { name: new RegExp(`${TEAM_KEY}-1`) })).toBeVisible()

    /*
      Migration 029 (estimates and dates), reached the way a user reaches it:
      the property panel on the issue's own page. Both fields commit on blur,
      so `press('Enter')` is the commit and the reload afterwards is the
      assertion that the commit reached a column rather than a state hook.
    */
    await row.click()
    await expect(page).toHaveURL(paths.issue(account.workspaceSlug, issueId))

    const estimate = page.getByRole('spinbutton', { name: 'Estimate' })
    await estimate.fill('5')
    await estimate.press('Enter')

    // `getByLabel` rather than `getByRole` for this one only: an
    // `<input type="date">` has no stable ARIA role across engines, so the
    // label association is the strongest thing there is to query by -- and
    // it still fails if the field loses its `<label for>`.
    const dueDate = page.getByLabel('Due date', { exact: true })
    await dueDate.fill('2030-01-15')
    await dueDate.press('Enter')

    await page.reload()
    await expect(page.getByRole('spinbutton', { name: 'Estimate' })).toHaveValue('5')
    await expect(page.getByLabel('Due date', { exact: true })).toHaveValue('2030-01-15')
  })

  test('a second account cannot see the first workspace or its issues', async ({
    page,
    browser,
  }) => {
    /*
      The most valuable test in this file.

      Tenant isolation through composite foreign keys on `workspace_id` is the
      property this whole schema is arranged around, and until now it had
      never been proven through a browser by a real second account. The db
      suite proves it against the repository layer; that is a different claim
      from "a person who signs up cannot read another person's work".

      Three refusals are asserted, and each one is a different mechanism:

        * the second account's own issue list, which must be empty -- the
          scoped list query, from the client that normally sends it;
        * the first workspace's URL, which must render "Workspace not found"
          with no rail and no data -- the shell refusing to draw a workspace
          it has no membership for;
        * the API, asked directly with the second account's cookie, both for
          the other workspace's slug and -- the sharper question -- for the
          other workspace's issue id under the caller's OWN slug. The second
          form is the one that cannot be answered by a membership check
          alone: it is the composite key, or nothing.
    */
    const alice = await signUp(page)
    const secret = newIssueTitle()
    const secretId = await createIssue(page, secret)
    await expect(page.getByRole('link', { name: new RegExp(secret) })).toBeVisible()

    // A separate browser context, not a sign-out. Bob gets his own cookie
    // jar, his own storage and his own JavaScript heap, so nothing he can
    // see was left behind by Alice's session rather than served to his.
    const bobContext = await browser.newContext()
    const bobPage = await bobContext.newPage()

    try {
      const bob = await signUp(bobPage)

      // Bob's workspace is empty. If tenant scoping were missing from the
      // list query, Alice's issue would be sitting in it.
      await expect(bobPage.getByRole('button', { name: 'Create the first issue' })).toBeVisible()
      await expect(bobPage.getByText(secret)).toHaveCount(0)

      await bobPage.goto(paths.issues(alice.workspaceSlug))
      await expect(bobPage.getByRole('heading', { name: 'Workspace not found' })).toBeVisible()
      await expect(bobPage.getByText(secret)).toHaveCount(0)

      // The deep link to the issue itself, which is what a leaked URL in a
      // chat message looks like. Same answer.
      await bobPage.goto(paths.issue(alice.workspaceSlug, secretId))
      await expect(bobPage.getByRole('heading', { name: 'Workspace not found' })).toBeVisible()
      await expect(bobPage.getByText(secret)).toHaveCount(0)

      // Now with the client out of the way. A screen that renders nothing is
      // not proof that the server sent nothing.
      const acrossWorkspaces = await graphql(bobContext.request, ISSUE_QUERY, {
        slug: alice.workspaceSlug,
        id: secretId,
      })

      // `{ issue: null }` and not `undefined`: the field was resolved and
      // answered nothing, rather than the query having failed to parse and
      // this assertion passing on an absent key.
      expect(acrossWorkspaces.body.data).toEqual({ issue: null })
      // NOT_FOUND, which is one of app/graphql/schema.py's PUBLIC_ERROR_CODES
      // -- a refusal the client is meant to see, not an internal exception
      // leaking through as a message.
      expect(acrossWorkspaces.body.errors?.[0]?.extensions?.code).toBe('NOT_FOUND')
      expect(acrossWorkspaces.text, "Alice's title reached Bob").not.toContain(secret)

      // The same id, under Bob's own slug -- a slug he is unquestionably
      // authorised for. Nothing about membership can refuse this one; only
      // the workspace_id in the WHERE clause can. It must be null, and it
      // must be null WITHOUT an error, because a different answer here from
      // the answer for an id that exists nowhere is itself the leak: it
      // confirms the issue is real.
      const idUnderOwnSlug = await graphql(bobContext.request, ISSUE_QUERY, {
        slug: bob.workspaceSlug,
        id: secretId,
      })

      expect(idUnderOwnSlug.body.data).toEqual({ issue: null })
      expect(idUnderOwnSlug.body.errors).toBeUndefined()
      expect(idUnderOwnSlug.text).not.toContain(secret)

      const nowhere = await graphql(bobContext.request, ISSUE_QUERY, {
        slug: bob.workspaceSlug,
        id: randomUUID(),
      })

      expect(
        idUnderOwnSlug.text,
        "an issue in another workspace is distinguishable from one that does not exist",
      ).toBe(nowhere.text)
    } finally {
      await bobContext.close()
    }
  })

  test('a signed-out visitor on a protected URL gets a refusal, not data', async ({
    page,
    browser,
  }) => {
    /*
      What this catches: the difference between "the screen looked empty" and
      "the server refused". A guard that redirects is only half of it -- the
      half an attacker skips by talking to /graphql directly -- so both halves
      are asserted, against a URL that really does have data behind it.
    */
    const account = await signUp(page)
    const title = newIssueTitle()
    const issueId = await createIssue(page, title)

    const visitorContext = await browser.newContext()
    const visitorPage = await visitorContext.newPage()

    try {
      await visitorPage.goto(paths.issue(account.workspaceSlug, issueId))

      // Redirected to sign-in, and to the path paths.ts owns rather than to a
      // literal spelled here twice.
      await expect(visitorPage).toHaveURL(publicPaths.login())
      await expect(visitorPage.getByRole('button', { name: 'Sign in' })).toBeVisible()

      // Not a crash: RouteError renders a stack trace's worth of nothing, and
      // an unhandled error boundary would show up here instead of the form.
      await expect(visitorPage.getByText(title)).toHaveCount(0)

      // And the API, with no cookie at all. UNAUTHENTICATED, no data, and
      // nothing about the issue in the bytes on the wire.
      const refused = await graphql(visitorContext.request, ISSUE_QUERY, {
        slug: account.workspaceSlug,
        id: issueId,
      })

      expect(refused.body.data).toEqual({ issue: null })
      expect(refused.body.errors?.[0]?.extensions?.code).toBe('UNAUTHENTICATED')
      expect(refused.text).not.toContain(title)

      // `me` is the deliberate exception and is asserted so that the rule
      // above cannot be "quietly made true" by making every query throw:
      // "who am I" answers null for nobody rather than erroring, because a
      // sign-in page has to be able to ask it.
      const viewer = await graphql(visitorContext.request, '{ me { id email } }', {})
      expect(viewer.body.data).toEqual({ me: null })
      expect(viewer.body.errors).toBeUndefined()
    } finally {
      await visitorContext.close()
    }
  })
})
