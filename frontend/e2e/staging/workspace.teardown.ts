import { test as teardown, expect } from '@playwright/test'
import type { APIRequestContext } from '@playwright/test'

import { workspaceSlug } from '../workspace'

/**
 * Best-effort cleanup of the per-run workspace after the staging suite.
 *
 * There is NO workspace / team / user / environment delete mutation in the
 * schema (94 mutations, none remove a workspace or a team), so the throwaway
 * workspace and user this run created cannot actually be deleted — the closest
 * available is archiving the issues, which empties the board. The inert empty
 * workspace and its throwaway user then persist; per-run isolation makes that
 * harmless (no later run reads them). This is the "archive everything if there
 * is no delete mutation" path, with the honest limit stated.
 */
// `first` is capped at 100 by the API. A single run creates far fewer issues
// than that, so one page is the whole workspace; archiving it empties the board.
const ISSUES = `query RunIssues($slug: String!) {
  issues(workspaceSlug: $slug, first: 100) { nodes { id } totalCount }
}`
const BULK_ARCHIVE = `mutation RunBulkArchive($input: IssueBulkArchiveInput!) {
  issueBulkArchive(input: $input) { count errors { field code message } }
}`

teardown('archive the run workspace content', async ({ playwright }) => {
  teardown.setTimeout(120_000)

  const baseURL = process.env.E2E_BASE_URL
  const slug = workspaceSlug()
  // Nothing to do if setup never ran (no file) — don't fail the run over it.
  if (!baseURL || slug === 'e2e-pending') return

  const api = await playwright.request.newContext({ baseURL, storageState: 'e2e/.auth/user.json' })
  const gql = async (
    query: string,
    variables: Record<string, unknown>,
    label: string,
  ): Promise<Record<string, any>> => {
    const response = await api.post('/graphql', { data: { query, variables } })
    expect(response.status(), `${label}: HTTP status`).toBe(200)
    const body = (await response.json()) as { data?: Record<string, any>; errors?: unknown }
    expect(body.errors ?? null, `${label}: GraphQL transport errors`).toBeNull()
    return body.data as Record<string, any>
  }

  const data = await gql(ISSUES, { slug }, 'issues')
  const ids: string[] = data.issues.nodes.map((node: { id: string }) => node.id)
  if (ids.length > 0) {
    await gql(BULK_ARCHIVE, { input: { workspaceSlug: slug, issueIds: ids } }, 'issueBulkArchive')
  }

  await (api as APIRequestContext).dispose()
})
