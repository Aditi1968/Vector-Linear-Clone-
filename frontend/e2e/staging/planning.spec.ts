import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'

import { expectMutation } from '../helpers'
import { workspaceSlug } from '../workspace'

/**
 * Planning cluster against live staging (the per-run workspace,
 * team key `QA`): projects, cycles, initiatives, releases, roadmap. Every test
 * mints its own data named `e2e <ISO timestamp>` and asserts on that record,
 * never on counts or "first row", and cleans up what the API allows.
 *
 * The `settle()` (networkidle) waits mirror issues.spec.ts: a control clicked
 * while the screen's own queries are still in flight can be lost when the list
 * resolving re-renders the tree. Waiting for the network to go idle after a
 * screen or dialog opens makes the next click deterministic.
 */

const WS = `/${workspaceSlug()}`

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** A row/card link's accessible name embeds the title; match it as a substring. */
function byTitle(title: string): RegExp {
  return new RegExp(escapeRegExp(title))
}

/** The row-select button on a list whose accessible name STARTS with the name. */
function byRowStart(name: string): RegExp {
  return new RegExp('^' + escapeRegExp(name))
}

/**
 * A unique 2-week window far in the future, as datetime-local strings. A team's
 * cycles may not overlap, and the shared workspace accumulates every run's
 * cycles, so a random day across a millennium makes a clash vanishingly
 * unlikely. (Copied from issues.spec.ts.)
 */
function futureCycleWindow(): { starts: string; ends: string } {
  const base = Date.UTC(2200, 0, 1)
  const start = new Date(base + Math.floor(Math.random() * 400_000) * 86_400_000)
  const end = new Date(start.getTime() + 13 * 86_400_000)
  const fmt = (d: Date) => d.toISOString().slice(0, 16)
  return { starts: fmt(start), ends: fmt(end) }
}

async function settle(page: Page): Promise<void> {
  await page.waitForLoadState('networkidle')
}

/** Create a project through the list dialog; leaves the browser on the list. */
async function createProject(page: Page, name: string): Promise<void> {
  await page.goto(`${WS}/projects`)
  await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible()
  await settle(page)
  await page.getByRole('button', { name: 'New project' }).click()
  const dialog = page.getByRole('dialog', { name: 'New project' })
  await dialog.getByRole('textbox', { name: 'Name' }).fill(name)
  await expect(dialog.getByRole('button', { name: 'Create project' })).toBeEnabled()
  const created = expectMutation(page, 'ProjectCreate')
  await dialog.getByRole('button', { name: 'Create project' }).click()
  await created
  await expect(page.getByRole('link', { name: byTitle(name) })).toBeVisible()
}

/** Best-effort delete of a project by name, so a linked helper leaves nothing. */
async function deleteProjectByName(page: Page, name: string): Promise<void> {
  await page.goto(`${WS}/projects`)
  await settle(page)
  const link = page.getByRole('link', { name: byTitle(name) })
  if (!(await link.isVisible().catch(() => false))) return
  await link.click()
  await settle(page)
  await page.getByRole('button', { name: `Actions for ${name}` }).click()
  const deleted = expectMutation(page, 'ProjectDelete')
  await page.getByRole('menuitem', { name: 'Delete project' }).click()
  await deleted
}

test.describe('/e2e-playground/projects', () => {
  test('Project lifecycle: create → update → milestones → teams → delete', async ({
    page,
  }) => {
    // A long lifecycle (8 mutations) against a cold Render free instance.
    test.setTimeout(120_000)
    const stamp = new Date().toISOString()
    const name = `e2e ${stamp}`

    await createProject(page, name)

    // Open the detail page.
    await page.getByRole('link', { name: byTitle(name) }).click()
    await settle(page)
    const actions = page.getByRole('button', { name: `Actions for ${name}` })
    await expect(actions).toBeVisible()

    // ---- ProjectUpdate: Actions → Edit project → Save changes
    await actions.click()
    await page.getByRole('menuitem', { name: 'Edit project' }).click()
    const editDialog = page.getByRole('dialog', { name: 'Edit project' })
    const desc = `desc ${stamp}`
    await editDialog.getByRole('textbox', { name: 'Description' }).fill(desc)
    const updated = expectMutation(page, 'ProjectUpdate')
    await editDialog.getByRole('button', { name: 'Save changes' }).click()
    await updated
    await expect(page.getByText(desc)).toBeVisible()

    // ---- ProjectMilestoneCreate: Add milestone
    const msName = `ms ${stamp}`
    await page.getByRole('button', { name: 'Add milestone' }).click()
    const msNameBox = page.getByRole('textbox', { name: 'Milestone name' })
    await expect(msNameBox).toBeVisible()
    await msNameBox.fill(msName)
    const msCreated = expectMutation(page, 'ProjectMilestoneCreate')
    await page.getByRole('button', { name: 'Add milestone' }).click()
    await msCreated
    await expect(page.getByRole('heading', { name: msName })).toBeVisible()

    // ---- ProjectMilestoneUpdate: Actions for {milestone} → Rename → Save
    const msRenamed = `${msName} renamed`
    await page.getByRole('button', { name: `Actions for ${msName}` }).click()
    await page.getByRole('menuitem', { name: 'Rename' }).click()
    const msEditBox = page.getByRole('textbox', { name: 'Milestone name' })
    await msEditBox.fill(msRenamed)
    const msUpdated = expectMutation(page, 'ProjectMilestoneUpdate')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await msUpdated
    await expect(page.getByRole('heading', { name: msRenamed })).toBeVisible()

    // ---- ProjectMilestoneDelete: Actions for {milestone} → Delete milestone
    await page.getByRole('button', { name: `Actions for ${msRenamed}` }).click()
    const msDeleted = expectMutation(page, 'ProjectMilestoneDelete')
    await page.getByRole('menuitem', { name: 'Delete milestone' }).click()
    await msDeleted
    await expect(page.getByRole('heading', { name: msRenamed })).toHaveCount(0)

    // ---- ProjectTeamAdd + ProjectTeamRemove (team QA)
    const teams = page.getByRole('region', { name: 'Teams' })
    await page.getByRole('button', { name: 'Add team' }).click()
    const teamAdded = expectMutation(page, 'ProjectTeamAdd')
    await page.getByRole('menuitem', { name: /QA/ }).click()
    await teamAdded
    await expect(teams.getByText('QA', { exact: true })).toBeVisible()

    const teamRemoved = expectMutation(page, 'ProjectTeamRemove')
    await teams.getByRole('button', { name: 'Remove QA' }).click()
    await teamRemoved
    await expect(teams.getByText('QA', { exact: true })).toHaveCount(0)

    // ---- ProjectDelete: Actions → Delete project
    await actions.click()
    const deleted = expectMutation(page, 'ProjectDelete')
    await page.getByRole('menuitem', { name: 'Delete project' }).click()
    await deleted
    await expect(page).toHaveURL(new RegExp(`${WS}/projects$`))
    // The list renders from the Apollo cache, which still holds the just-deleted
    // row; a reload re-runs ProjectList from the network, where it is gone.
    await page.reload()
    await settle(page)
    await expect(page.getByRole('link', { name: byTitle(name) })).toHaveCount(0)
  })
})

test.describe('/e2e-playground/cycles', () => {
  test('Cycle: create → open → update → delete', async ({ page }) => {
    const stamp = new Date().toISOString()
    const cycleName = `e2e cycle ${stamp}`

    await page.goto(`${WS}/cycles`)
    await expect(page.getByRole('heading', { name: 'Cycles' })).toBeVisible()
    await settle(page)

    // Cycles are team-scoped. Select the QA team by its option value (the label
    // is "QA · <name>", whose exact text we do not want to hard-code). The
    // selection is screen state, not a URL param, so it is redone after reload.
    const selectQaTeam = async (): Promise<void> => {
      const teamSelect = page.getByRole('combobox', { name: 'Team' })
      const qaValue = await teamSelect
        .locator('option', { hasText: 'QA' })
        .first()
        .getAttribute('value')
      if (qaValue) {
        await teamSelect.selectOption(qaValue)
        await settle(page)
      }
    }
    await selectQaTeam()

    // ---- CycleCreate (name + a unique number + the datetime-local window)
    await page.getByRole('button', { name: 'New cycle' }).click()
    const dialog = page.getByRole('dialog', { name: 'New cycle' })
    await dialog.getByRole('textbox', { name: 'Name (optional)' }).fill(cycleName)
    // The dialog prefills the number as max(existing)+1, computed from whatever
    // is loaded when it opens; two cycle-creating tests share this run's QA team
    // and both get "1" before the list settles, so the second collides on the
    // per-team-unique number. Set a fixed, distinct number instead (the
    // workspace is fresh per run and only these two tests create cycles):
    // 101 here, 202 in issues.spec.ts IssueSetCycle. (Valid range is 1..9999.)
    await dialog.getByRole('spinbutton', { name: 'Number' }).fill('101')
    const { starts, ends } = futureCycleWindow()
    await dialog.getByRole('textbox', { name: 'Starts' }).fill(starts)
    await dialog.getByRole('textbox', { name: 'Ends' }).fill(ends)
    await expect(dialog.getByRole('button', { name: 'Create cycle' })).toBeEnabled()
    const created = expectMutation(page, 'CycleCreate')
    await dialog.getByRole('button', { name: 'Create cycle' }).click()
    await created

    // The team-scoped cycle list is cache-served and can lag the create under
    // concurrent load; a reload runs CycleList fresh. Re-select QA, then open
    // the new cycle from its row link.
    await page.reload()
    await settle(page)
    await selectQaTeam()
    await page.getByRole('link', { name: byTitle(cycleName) }).click()
    await settle(page)
    await expect(page.getByRole('button', { name: `Actions for ${cycleName}` })).toBeVisible()

    // ---- CycleUpdate: Actions → Edit cycle → Save changes
    await page.getByRole('button', { name: `Actions for ${cycleName}` }).click()
    await page.getByRole('menuitem', { name: 'Edit cycle' }).click()
    const editDialog = page.getByRole('dialog', { name: 'Edit cycle' })
    const renamed = `${cycleName} renamed`
    await editDialog.getByRole('textbox', { name: 'Name (optional)' }).fill(renamed)
    const updated = expectMutation(page, 'CycleUpdate')
    await editDialog.getByRole('button', { name: 'Save changes' }).click()
    await updated
    await expect(page.getByRole('button', { name: `Actions for ${renamed}` })).toBeVisible()

    // ---- CycleDelete: Actions → Delete cycle
    await page.getByRole('button', { name: `Actions for ${renamed}` }).click()
    const deleted = expectMutation(page, 'CycleDelete')
    await page.getByRole('menuitem', { name: 'Delete cycle' }).click()
    await deleted
    await expect(page).toHaveURL(new RegExp(`${WS}/cycles$`))
  })
})

test.describe('/e2e-playground/initiatives', () => {
  test('Initiative: create → update → link/unlink project → post update → delete', async ({
    page,
  }) => {
    // Creates a helper project, the full initiative lifecycle, then cleans the
    // project up: many round trips against a cold Render free instance.
    test.setTimeout(150_000)
    const stamp = new Date().toISOString()
    const name = `e2e initiative ${stamp}`
    const projectName = `e2e initproj ${stamp}`

    // A project to link. Created first so the initiatives workspace-context
    // query (run on page load) already includes it.
    await createProject(page, projectName)

    await page.goto(`${WS}/initiatives`)
    await expect(page.getByRole('heading', { name: 'Initiatives' })).toBeVisible()
    await settle(page)

    // ---- InitiativeCreate
    await page.getByRole('button', { name: 'New initiative' }).click()
    const dialog = page.getByRole('dialog', { name: 'New initiative' })
    await dialog.getByRole('textbox', { name: 'Initiative name' }).fill(name)
    const created = expectMutation(page, 'InitiativeCreate')
    await dialog.getByRole('button', { name: 'Create initiative' }).click()
    await created

    // Select my initiative's row so the detail panel shows it.
    await page.getByRole('button', { name: byRowStart(name) }).click()
    await expect(page.getByRole('heading', { name, level: 2 })).toBeVisible()

    // ---- InitiativeProjectAdd
    await page.getByRole('combobox', { name: 'Add a project' }).selectOption({
      label: projectName,
    })
    const projAdded = expectMutation(page, 'InitiativeProjectAdd')
    await page.getByRole('button', { name: 'Add', exact: true }).click()
    await projAdded
    await expect(page.getByRole('link', { name: projectName })).toBeVisible()

    // ---- InitiativeProjectRemove
    const projRemoved = expectMutation(page, 'InitiativeProjectRemove')
    await page.getByRole('button', { name: `Remove ${projectName} from ${name}` }).click()
    await projRemoved
    await expect(page.getByRole('link', { name: projectName })).toHaveCount(0)

    // ---- InitiativeUpdatePost (the only way to set health: a health + a note)
    const note = `update ${stamp}`
    await page.getByRole('textbox', { name: 'What changed' }).fill(note)
    const posted = expectMutation(page, 'InitiativeUpdatePost')
    await page.getByRole('button', { name: 'Post update' }).click()
    await posted
    await expect(page.getByText(note)).toBeVisible()

    // ---- InitiativeUpdate: row menu → Edit initiative... → Save changes
    const renamed = `${name} renamed`
    await page.getByRole('button', { name: `Actions on ${name}` }).click()
    await page.getByRole('menuitem', { name: 'Edit initiative...' }).click()
    const editDialog = page.getByRole('dialog', { name: 'Edit initiative' })
    await editDialog.getByRole('textbox', { name: 'Initiative name' }).fill(renamed)
    const updated = expectMutation(page, 'InitiativeUpdate')
    await editDialog.getByRole('button', { name: 'Save changes' }).click()
    await updated
    await expect(page.getByRole('button', { name: byRowStart(renamed) })).toBeVisible()

    // ---- InitiativeDelete
    await page.getByRole('button', { name: `Actions on ${renamed}` }).click()
    const deleted = expectMutation(page, 'InitiativeDelete')
    await page.getByRole('menuitem', { name: 'Delete initiative' }).click()
    await deleted
    await expect(page.getByRole('button', { name: byRowStart(renamed) })).toHaveCount(0)

    // Clean up the helper project.
    await deleteProjectByName(page, projectName)
  })

  test('Initiative: set parent → clear parent', async ({ page }) => {
    const stamp = new Date().toISOString()
    const parentName = `e2e parent ${stamp}`
    const childName = `e2e child ${stamp}`

    await page.goto(`${WS}/initiatives`)
    await expect(page.getByRole('heading', { name: 'Initiatives' })).toBeVisible()
    await settle(page)

    for (const initiativeName of [parentName, childName]) {
      await page.getByRole('button', { name: 'New initiative' }).click()
      const dialog = page.getByRole('dialog', { name: 'New initiative' })
      await dialog.getByRole('textbox', { name: 'Initiative name' }).fill(initiativeName)
      const created = expectMutation(page, 'InitiativeCreate')
      await dialog.getByRole('button', { name: 'Create initiative' }).click()
      await created
    }

    // Select the child; nest it under the parent.
    await page.getByRole('button', { name: byRowStart(childName) }).click()
    await expect(page.getByRole('heading', { name: childName, level: 2 })).toBeVisible()

    await page.getByRole('combobox', { name: 'Nest under' }).selectOption({ label: parentName })
    const setParent = expectMutation(page, 'InitiativeSetParent')
    await page.getByRole('button', { name: 'Nest', exact: true }).click()
    await setParent
    const moveToTop = page.getByRole('button', { name: 'Move to top level' })
    await expect(moveToTop).toBeVisible()

    // Clear the parent.
    const clearParent = expectMutation(page, 'InitiativeClearParent')
    await moveToTop.click()
    await clearParent
    await expect(page.getByRole('button', { name: 'Move to top level' })).toHaveCount(0)

    // Clean up both.
    for (const initiativeName of [childName, parentName]) {
      await page.getByRole('button', { name: `Actions on ${initiativeName}` }).click()
      const deleted = expectMutation(page, 'InitiativeDelete')
      await page.getByRole('menuitem', { name: 'Delete initiative' }).click()
      await deleted
    }
  })
})

test.describe('/e2e-playground/releases', () => {
  test('Release: create → set status → delete', async ({ page }) => {
    const stamp = new Date().toISOString()
    const version = `e2e-${stamp}`

    await page.goto(`${WS}/releases`)
    await expect(page.getByRole('heading', { name: 'Releases' })).toBeVisible()
    await settle(page)

    await page.getByRole('button', { name: 'Cut a release' }).click()
    const dialog = page.getByRole('dialog', { name: 'Cut a release' })

    // A release names an environment and a GitHub repository, both foreign keys
    // the database must resolve. When the workspace has neither declared, the
    // form renders a block instead of fields and no release can be cut.
    const blocked = dialog.getByText(
      /no GitHub repository connected|no environment to deploy/,
    )
    if (await blocked.isVisible().catch(() => false)) {
      const reason = (await blocked.textContent()) ?? 'Release prerequisites missing.'
      test.skip(true, `Releases blocked on staging: ${reason.trim()}`)
      return
    }

    // Both pickers are present. Fill the form and cut.
    await dialog.getByRole('textbox', { name: 'Version' }).fill(version)
    await dialog
      .getByRole('combobox', { name: 'Environment' })
      .selectOption({ index: 1 })
    await dialog
      .getByRole('combobox', { name: 'Repository' })
      .selectOption({ index: 1 })
    // A full 40-char lowercase-hex SHA (the field's native pattern); the server
    // still has to resolve it to a real commit in the chosen repository.
    await dialog.getByRole('textbox', { name: 'Commit', exact: true }).fill('a'.repeat(40))

    const created = expectMutation(page, 'ReleaseCreate')
    await dialog.getByRole('button', { name: 'Cut release' }).click()
    await created

    // The new release is selected; the panel shows it with status actions.
    await expect(page.getByRole('heading', { name: version, level: 2 })).toBeVisible()

    // ---- ReleaseStatusSet (PENDING → one of its legal next states)
    const status = expectMutation(page, 'ReleaseStatusSet')
    await page.getByRole('button', { name: /^Mark / }).first().click()
    await status

    // ---- ReleaseDelete
    const deleted = expectMutation(page, 'ReleaseDelete')
    await page.getByRole('button', { name: 'Delete release' }).click()
    await deleted
  })
})

test.describe('/e2e-playground/roadmap', () => {
  test('Roadmap renders (view only — no mutation controls)', async ({ page }) => {
    await page.goto(`${WS}/roadmap`)
    await expect(page.getByRole('heading', { name: 'Roadmap', level: 1 })).toBeVisible()
    await settle(page)

    // The roadmap is a read-only view over initiatives + projects: the schema
    // stores one date each and no start date, so there is nothing to mutate
    // here. Assert it rendered one of its two legitimate states.
    const populated = page.getByText(/There is no start date in the schema/)
    const empty = page.getByRole('heading', { name: 'Nothing to schedule yet' })
    await expect(populated.or(empty)).toBeVisible()
  })
})
