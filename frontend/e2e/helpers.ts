import { expect } from '@playwright/test'
import type { Page, Response } from '@playwright/test'

/**
 * Assert that a GraphQL mutation (or query) by `operationName` succeeds.
 *
 * Call it *before* the click that triggers the request, so the listener is
 * registered in time, then await the returned promise *after* the click:
 *
 *     const done = expectMutation(page, 'OnboardingTeamCreate')
 *     await page.getByRole('button', { name: 'Create team' }).click()
 *     const data = await done
 *
 * It waits for the POST to /graphql whose `operationName` matches, asserts a
 * 200 with no top-level `errors`, and returns the `data` object. A GraphQL
 * error or a non-200 fails the test with the operation name in the message,
 * which is the whole point: a button that "looks like it worked" but whose
 * mutation came back with errors is exactly what this suite is hunting.
 */
export function expectMutation<T = Record<string, unknown>>(
  page: Page,
  operationName: string,
): Promise<T> {
  return page
    .waitForResponse(
      (response) =>
        response.url().includes('/graphql') &&
        response.request().method() === 'POST' &&
        requestOperationName(response) === operationName,
    )
    .then(async (response) => {
      expect(response.status(), `${operationName}: HTTP status`).toBe(200)
      const body = (await response.json()) as { data?: T; errors?: unknown }
      expect(body.errors ?? null, `${operationName}: GraphQL errors`).toBeNull()
      return body.data as T
    })
}

/** The `operationName` Apollo put in a request body, or undefined. */
function requestOperationName(response: Response): string | undefined {
  try {
    return (JSON.parse(response.request().postData() ?? '{}') as { operationName?: string })
      .operationName
  } catch {
    return undefined
  }
}
