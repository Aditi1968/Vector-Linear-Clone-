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
      // Vector mutation payloads return field-level validation failures in a
      // nested `errors` array (e.g. `cycleCreate.errors`) *alongside* HTTP 200
      // and a null top-level `errors`. A test that checks only the top level
      // treats a rejected write as success and then times out downstream
      // waiting for an effect that never happened. Surface the payload error
      // here so the test fails fast with the server's own message.
      expect(firstPayloadError(body.data), `${operationName}: payload-level errors`).toBeNull()
      return body.data as T
    })
}

/**
 * Wait for a query that a mutation reran via Apollo `refetchQueries`.
 *
 * Several list mutations (saved views, favorites) don't update the cache in
 * place; they rerun the list query by name, which is a *separate* round-trip
 * that lands after the mutation's own response. A "row appears / disappears"
 * assertion made right after `await <mutation>` therefore races that refetch,
 * and under concurrent load against a small instance the refetch can exceed
 * the default assertion timeout. Awaiting the refetch first makes the
 * assertion deterministic without widening any timeout.
 *
 * Same wait + 200/no-errors check as `expectMutation`; named for intent. Set
 * it up BEFORE the click (alongside the mutation waiter) and await it after.
 *
 * Pass `payloadContains` to wait for the *right* refetch, not just any response
 * for this query. Apollo can resolve an earlier in-flight copy of the same list
 * query (e.g. the initial empty load) after this waiter is armed but before the
 * refetch that carries the new row. Waiting for `operationName` alone resolves
 * on that stale response, so the row assertion still races the real refetch.
 * The predicate runs against the response's `data` and only a body that
 * satisfies it resolves the wait — making "row appears after mutation"
 * deterministic without widening a timeout.
 */
export function expectRefetch<T = Record<string, unknown>>(
  page: Page,
  operationName: string,
  payloadContains?: (data: T) => boolean,
): Promise<T> {
  return page
    .waitForResponse(async (response) => {
      if (
        !response.url().includes('/graphql') ||
        response.request().method() !== 'POST' ||
        requestOperationName(response) !== operationName
      ) {
        return false
      }
      if (!payloadContains) return true
      try {
        const body = (await response.json()) as { data?: T; errors?: unknown }
        return body.errors == null && body.data != null && payloadContains(body.data)
      } catch {
        return false
      }
    })
    .then(async (response) => {
      expect(response.status(), `${operationName}: HTTP status`).toBe(200)
      const body = (await response.json()) as { data?: T; errors?: unknown }
      expect(body.errors ?? null, `${operationName}: GraphQL errors`).toBeNull()
      // Vector mutation payloads return field-level validation failures in a
      // nested `errors` array (e.g. `cycleCreate.errors`) *alongside* HTTP 200
      // and a null top-level `errors`. A test that checks only the top level
      // treats a rejected write as success and then times out downstream
      // waiting for an effect that never happened. Surface the payload error
      // here so the test fails fast with the server's own message.
      expect(firstPayloadError(body.data), `${operationName}: payload-level errors`).toBeNull()
      return body.data as T
    })
}

/** The first non-empty `errors` array's messages anywhere in a payload, or null. */
function firstPayloadError(data: unknown): string | null {
  if (Array.isArray(data)) {
    for (const item of data) {
      const found = firstPayloadError(item)
      if (found) return found
    }
  } else if (data && typeof data === 'object') {
    for (const [key, value] of Object.entries(data)) {
      if (key === 'errors' && Array.isArray(value) && value.length > 0) {
        return value
          .map((e) =>
            e && typeof e === 'object' && 'message' in e
              ? String((e as { message: unknown }).message)
              : JSON.stringify(e),
          )
          .join('; ')
      }
      const found = firstPayloadError(value)
      if (found) return found
    }
  }
  return null
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
