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

  test('LabelGroupUpdate renames a group', async ({ page }) => {
    await page.goto(`/${SLUG}/label-groups`)
    const name = `e2e ${stamp()}`
    const renamed = `${name} renamed`

    await page.getByRole('button', { name: 'New group' }).click()
    const dialog = page.getByRole('dialog', { name: 'New label group' })
    await dialog.getByRole('textbox', { name: 'Group name' }).fill(name)
    const created = expectMutation(page, 'LabelGroupCreate')
    await dialog.getByRole('button', { name: 'Create group' }).click()
    await created

    const row = page.getByRole('button', { name: `${name} 0 labels`, exact: true })
    await expect(row).toBeVisible()
    const region = page.getByRole('region', { name, exact: true })
    if (!(await region.isVisible())) await row.click()
    await expect(region).toBeVisible()

    // Edit group… → rename → Save changes (LabelGroupUpdate).
    await region.getByRole('button', { name: /^Edit group/ }).click()
    const editDialog = page.getByRole('dialog', { name: 'Edit label group' })
    await editDialog.getByRole('textbox', { name: 'Group name' }).fill(renamed)
    const updated = expectMutation(page, 'LabelGroupUpdate')
    await editDialog.getByRole('button', { name: 'Save changes' }).click()
    await updated

    const renamedRow = page.getByRole('button', { name: `${renamed} 0 labels`, exact: true })
    await expect(renamedRow).toBeVisible()

    // Clean up.
    const renamedRegion = page.getByRole('region', { name: renamed, exact: true })
    if (!(await renamedRegion.isVisible())) await renamedRow.click()
    const deleted = expectMutation(page, 'LabelGroupDelete')
    await renamedRegion.getByRole('button', { name: 'Delete group' }).click()
    await deleted
    await expect(renamedRow).toBeHidden()
  })

  test('LabelSetGroup moves a label into a group and back out', async ({ page }) => {
    // LabelSetGroup needs an ungrouped workspace label. The only label-create
    // UI is the issue Labels panel, so mint one there first (LabelCreate), then
    // group and ungroup it on the label-groups screen.
    test.setTimeout(120_000)
    const s = stamp()
    const labelName = `e2e-lbl-${s.replace(/[:.]/g, '-')}`
    const groupName = `e2e grp ${s}`

    // --- mint an ungrouped label via a throwaway issue ---
    await page.goto(`/${SLUG}/issues`)
    await page.waitForLoadState('networkidle')
    await page.getByRole('button', { name: 'New issue' }).click()
    await page.waitForLoadState('networkidle')
    await page.getByRole('textbox', { name: 'Title' }).fill(`e2e lbl-host ${s}`)
    const issueCreated = expectMutation(page, 'IssueCreate')
    await page.getByRole('button', { name: 'Create issue' }).click()
    await issueCreated
    await page.getByRole('link', { name: new RegExp(`e2e lbl-host ${s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) }).click()
    const detail = page.getByRole('complementary', { name: 'Issue detail' })
    const labels = detail.getByRole('region', { name: 'Labels' })
    await labels.getByRole('button', { name: 'Add', exact: true }).click()
    await page.getByRole('menuitem', { name: 'New label' }).click()
    await labels.getByRole('textbox', { name: 'New label name' }).fill(labelName)
    const labelCreated = expectMutation(page, 'LabelCreate')
    await labels.getByRole('button', { name: 'Create', exact: true }).click()
    await labelCreated
    // Detach it from the issue so it is an ungrouped, unattached workspace label.
    const detached = expectMutation(page, 'IssueLabelDetach')
    await labels.getByRole('button', { name: `Remove ${labelName}` }).click()
    await detached

    // --- group it, then ungroup it ---
    await page.goto(`/${SLUG}/label-groups`)
    await page.getByRole('button', { name: 'New group' }).click()
    const dialog = page.getByRole('dialog', { name: 'New label group' })
    await dialog.getByRole('textbox', { name: 'Group name' }).fill(groupName)
    const groupCreated = expectMutation(page, 'LabelGroupCreate')
    await dialog.getByRole('button', { name: 'Create group' }).click()
    await groupCreated

    const row = page.getByRole('button', { name: `${groupName} 0 labels`, exact: true })
    await expect(row).toBeVisible()
    const region = page.getByRole('region', { name: groupName, exact: true })
    if (!(await region.isVisible())) await row.click()

    // Add to group (LabelSetGroup: label → this group).
    await region.getByRole('combobox', { name: 'Ungrouped label' }).selectOption({ label: labelName })
    const added = expectMutation(page, 'LabelSetGroup')
    await region.getByRole('button', { name: 'Add to group' }).click()
    await added
    await expect(region.getByRole('button', { name: `Remove ${labelName}` })).toBeVisible()

    // Remove from group (LabelSetGroup: label → null).
    const removed = expectMutation(page, 'LabelSetGroup')
    await region.getByRole('button', { name: `Remove ${labelName}` }).click()
    await removed

    // Clean up the group (the label persists — labels have no delete UI).
    const deleted = expectMutation(page, 'LabelGroupDelete')
    await region.getByRole('button', { name: 'Delete group' }).click()
    await deleted
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

  test('IssueTemplateUpdate renames a template', async ({ page }) => {
    await page.goto(`/${SLUG}/templates`)
    const s = stamp()
    const name = `e2e ${s}`
    const renamed = `${name} renamed`

    await page.getByRole('button', { name: 'New template' }).click()
    const createDialog = page.getByRole('dialog', { name: 'New template' })
    await createDialog.getByRole('textbox', { name: 'Template name' }).fill(name)
    await createDialog.getByRole('textbox', { name: 'Issue title' }).fill(`e2e issue ${s}`)
    const created = expectMutation(page, 'IssueTemplateCreate')
    await createDialog.getByRole('button', { name: 'Create template' }).click()
    await created
    await expect(page.getByRole('button', { name: `Actions on ${name}` })).toBeVisible()

    // Actions on {name} → Edit template... → rename → Save changes.
    await page.getByRole('button', { name: `Actions on ${name}` }).click()
    await page.getByRole('menuitem', { name: /^Edit template/ }).click()
    const editDialog = page.getByRole('dialog', { name: 'Edit template' })
    await editDialog.getByRole('textbox', { name: 'Template name' }).fill(renamed)
    const updated = expectMutation(page, 'IssueTemplateUpdate')
    await editDialog.getByRole('button', { name: 'Save changes' }).click()
    await updated

    const renamedActions = page.getByRole('button', { name: `Actions on ${renamed}` })
    await expect(renamedActions).toBeVisible()

    // Clean up.
    await renamedActions.click()
    const deleted = expectMutation(page, 'IssueTemplateDelete')
    await page.getByRole('menuitem', { name: 'Delete template' }).click()
    await deleted
    await expect(renamedActions).toBeHidden()
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
