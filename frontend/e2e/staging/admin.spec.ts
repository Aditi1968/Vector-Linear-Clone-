import { test, expect } from '@playwright/test'

import { expectMutation } from '../helpers'

/**
 * Admin cluster against live staging: members, environments, label groups,
 * templates, settings. Each test mints its own data named `e2e <ISO stamp>`
 * and asserts on that record, never on counts or first-row (Phase-3 rules).
 *
 * `expectMutation(page, 'Op')` is created *before* the click and awaited
 * *after*, so a button that "looks like it worked" but whose GraphQL came back
 * with errors fails here with the op name.
 */

const SLUG = 'e2e-playground'

/** A fresh ISO timestamp — unique per call, safe inside an accessible name. */
const stamp = () => new Date().toISOString()

test.describe('/members', () => {
  test('MemberInvitationCreate then MemberInvitationRevoke round-trip', async ({ page }) => {
    await page.goto(`/${SLUG}/members`)

    // ISO stamp carries ':' and '.', which are not valid in an email address,
    // so the local part uses a '-'-only variant. Still unique per run.
    const email = `e2e+${stamp().replace(/[:.]/g, '-')}@example.com`

    const emailField = page.getByRole('textbox', { name: 'Email address' })
    await expect(emailField).toBeVisible()
    await emailField.fill(email)

    const created = expectMutation(page, 'MemberInvitationCreate')
    await page.getByRole('button', { name: 'Create invite link' }).click()
    await created

    // The outstanding-invitation row carries a per-email revoke button — this
    // is the row appearing. (The one-shot "Links to send" block also shows the
    // email, and it survives a revoke, so the revoke button is what we track.)
    const revoke = page.getByRole('button', { name: `Revoke the invitation for ${email}` })
    await expect(revoke).toBeVisible()

    const revoked = expectMutation(page, 'MemberInvitationRevoke')
    await revoke.click()
    await revoked

    await expect(revoke).toBeHidden()
  })

  test('owner role menu renders; last-owner is not demotable (observable, safe)', async ({
    page,
  }) => {
    // MemberRoleUpdate / MemberRemove need a second member: this workspace has
    // one owner, and removing or demoting the only owner would lock the shared
    // test account out of staging. So those mutations are inventory-only
    // (INVENTORY rule 5); here we only read the menu, firing nothing.
    await page.goto(`/${SLUG}/members`)

    const actions = page.getByRole('button', { name: 'Actions for E2E Tester' })
    await expect(actions).toBeVisible()
    await actions.click()

    const menu = page.getByRole('menu', { name: 'Actions for E2E Tester' })
    await expect(menu.getByRole('menuitem', { name: 'Owner (current)' })).toBeDisabled()

    await page.keyboard.press('Escape')
  })
})

test.describe('/environments', () => {
  test('EnvironmentCreate adds the row', async ({ page }) => {
    await page.goto(`/${SLUG}/environments`)

    // Environments are create-only — the API offers no rename or delete, so
    // there is nothing to clean up (and nothing to assert gone).
    const name = `e2e ${stamp()}`

    await page.getByRole('button', { name: 'New environment' }).click()
    const dialog = page.getByRole('dialog', { name: 'Add environment' })
    await dialog.getByRole('textbox', { name: 'Name' }).fill(name)

    const done = expectMutation(page, 'EnvironmentCreate')
    await dialog.getByRole('button', { name: 'Add environment' }).click()
    await done

    await expect(
      page.getByRole('list', { name: 'Environments' }).getByText(name, { exact: true }),
    ).toBeVisible()
  })
})

test.describe('/label-groups', () => {
  test('LabelGroupCreate then LabelGroupDelete round-trip', async ({ page }) => {
    await page.goto(`/${SLUG}/label-groups`)

    const name = `e2e ${stamp()}`

    await page.getByRole('button', { name: 'New group' }).click()
    const dialog = page.getByRole('dialog', { name: 'New label group' })
    await dialog.getByRole('textbox', { name: 'Group name' }).fill(name)

    const created = expectMutation(page, 'LabelGroupCreate')
    await dialog.getByRole('button', { name: 'Create group' }).click()
    await created

    // The new group shows as a toggle row "<name> 0 labels" in the list.
    const row = page.getByRole('button', { name: `${name} 0 labels`, exact: true })
    await expect(row).toBeVisible()

    // Selecting it opens the detail region (named after the group) that holds
    // the delete button. Creation may auto-select it; only click if it didn't.
    const region = page.getByRole('region', { name, exact: true })
    if (!(await region.isVisible())) await row.click()
    await expect(region).toBeVisible()

    const deleted = expectMutation(page, 'LabelGroupDelete')
    await region.getByRole('button', { name: 'Delete group' }).click()
    await deleted

    await expect(row).toBeHidden()
  })
})

test.describe('/templates', () => {
  test('IssueTemplateCreate, IssueCreateFromTemplate (team QA), IssueTemplateDelete', async ({
    page,
  }) => {
    await page.goto(`/${SLUG}/templates`)

    const s = stamp()
    const tplName = `e2e ${s}`
    const issueTitle = `e2e issue ${s}`

    // --- IssueTemplateCreate ---
    await page.getByRole('button', { name: 'New template' }).click()
    const createDialog = page.getByRole('dialog', { name: 'New template' })
    await createDialog.getByRole('textbox', { name: 'Template name' }).fill(tplName)
    // Leave Team = "Any team" so filing from it must ask for a team.
    await createDialog.getByRole('textbox', { name: 'Issue title' }).fill(issueTitle)

    const created = expectMutation(page, 'IssueTemplateCreate')
    await createDialog.getByRole('button', { name: 'Create template' }).click()
    await created

    const actions = page.getByRole('button', { name: `Actions on ${tplName}` })
    await expect(actions).toBeVisible()

    // --- IssueCreateFromTemplate ---
    await page.getByRole('button', { name: `New issue from ${tplName}` }).click()
    const fileDialog = page.getByRole('dialog', { name: 'New issue from template' })
    await fileDialog.getByRole('combobox', { name: 'Team' }).selectOption({ label: 'QA · QA' })

    const filed = expectMutation(page, 'IssueCreateFromTemplate')
    await fileDialog.getByRole('button', { name: 'Create issue' }).click()
    await filed

    // Success surfaces as a link to the filed issue: "<KEY>-<n> · <title>".
    await expect(page.getByRole('link', { name: issueTitle })).toBeVisible()

    // --- IssueTemplateDelete (clean up the template) ---
    await actions.click()
    const deleted = expectMutation(page, 'IssueTemplateDelete')
    await page.getByRole('menuitem', { name: 'Delete template' }).click()
    await deleted

    await expect(actions).toBeHidden()
  })
})

test.describe('/settings', () => {
  test('WorkspaceGithubAutomationSet — only when GitHub is connected', async ({ page }) => {
    await page.goto(`/${SLUG}/settings`)

    await expect(page.getByRole('heading', { name: 'GitHub', level: 2 })).toBeVisible()

    // The integration check resolves into either a "Connect GitHub" OAuth link
    // (disconnected) or the repo/automation controls (connected). Let it settle.
    const connect = page.getByRole('link', { name: 'Connect GitHub' })
    await connect.waitFor({ state: 'visible', timeout: 15_000 }).catch(() => {})

    test.skip(
      await connect.isVisible(),
      'GitHub is not connected on this staging workspace; the PR-automation controls ' +
        '(WorkspaceGithubAutomationSet) are not rendered, so there is nothing to exercise.',
    )

    // Connected path (unreachable on this staging, which is disconnected): the
    // PR-automation enable toggle fires WorkspaceGithubAutomationSet. Left as a
    // guard rather than fabricated selectors, since it cannot be observed here.
    const automation = page.getByRole('checkbox', { name: /pull request/i })
    const set = expectMutation(page, 'WorkspaceGithubAutomationSet')
    await automation.click()
    await set
  })
})
