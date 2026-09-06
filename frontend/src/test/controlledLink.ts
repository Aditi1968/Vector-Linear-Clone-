import { ApolloLink } from '@apollo/client'
import { act, waitFor } from '@testing-library/react'
import { Observable } from 'rxjs'
import type { Subscriber } from 'rxjs'
import { expect } from 'vitest'

/**
 * An Apollo link that answers nothing until a test tells it to.
 *
 * ## Why not `MockLink`/`MockedProvider`
 *
 * Apollo ships one (`@apollo/client/testing`, with `MockedProvider` moved to
 * `@apollo/client/testing/react` in v4) and it is a fine tool for "given this
 * query, return this result". Three of the things this suite has to prove are
 * outside what it can do:
 *
 *   1. **Assert what was sent, not merely that something matched.** `MockLink`
 *      matches a request against a mock and reports a mismatch as a console
 *      warning plus a thrown "No more mocked responses" error. A pagination
 *      test that asserts the *cursor* has to read the variables that actually
 *      went out -- and a mock that silently failed to match would otherwise
 *      surface as a rendering failure several assertions later, with a cause
 *      buried in stderr.
 *   2. **Observe the in-flight window deterministically.** `MockLink`'s
 *      default delay is `realisticDelay()`, a *random* 20-50ms. Proving that
 *      loaded rows stay on screen while the next page loads means asserting
 *      between dispatch and response, and a randomised timer makes that a
 *      race. Here nothing resolves until `resolve()` is called, so the window
 *      is exactly as long as the test needs.
 *   3. **Dispatch the same cursor twice.** A `MockLink` mock is consumed on
 *      first use (`maxUsageCount` defaults to 1), so a second identical
 *      request errors rather than being answered -- which is the opposite of
 *      the duplicate-row scenario under test.
 *
 * What is emphatically *not* mocked here is anything above the link: the
 * client, the `InMemoryCache` and the `issues` field policy under test are all
 * the real ones (see ./render.tsx). This class replaces the network and
 * nothing else.
 */

/** One operation as it went over the wire. */
export interface RecordedOperation {
  /** `IssueList`, `IssueDetail`, `IssueCreate`. */
  name: string
  /** Exactly the variables Apollo sent, snapshotted at dispatch. */
  variables: Record<string, unknown>
}

interface PendingOperation extends RecordedOperation {
  subscriber: Subscriber<ApolloLink.Result>
}

/**
 * Let React and Apollo finish reacting to something.
 *
 * A macrotask rather than a microtask: rxjs schedules parts of Apollo's result
 * delivery on `asapScheduler`, and React 19 flushes passive effects on a task
 * boundary. Awaiting only `Promise.resolve()` lands before both often enough
 * to be intermittent, which is worse than being slow.
 */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 0)
    })
  })
}

export class ControlledLink extends ApolloLink {
  /** Every operation dispatched, oldest first. Never pruned. */
  readonly operations: RecordedOperation[] = []

  private readonly pending: PendingOperation[] = []

  override request(operation: ApolloLink.Operation): Observable<ApolloLink.Result> {
    const name = operation.operationName ?? '<anonymous>'
    // Snapshotted rather than referenced: Apollo reuses the variables object
    // across a `fetchMore`, so holding the reference would make an assertion
    // about the first page read the second page's cursor.
    const variables: Record<string, unknown> = { ...operation.variables }

    this.operations.push({ name, variables })

    return new Observable<ApolloLink.Result>((subscriber) => {
      const entry: PendingOperation = { name, variables, subscriber }
      this.pending.push(entry)

      return () => {
        const index = this.pending.indexOf(entry)

        if (index !== -1) {
          this.pending.splice(index, 1)
        }
      }
    })
  }

  /** Every dispatch of one operation, oldest first. */
  operationsNamed(name: string): RecordedOperation[] {
    return this.operations.filter((operation) => operation.name === name)
  }

  /** How many dispatches of one operation there have been in total. */
  countOf(name: string): number {
    return this.operationsNamed(name).length
  }

  /** How many requests of this name are waiting for an answer right now. */
  inFlightCount(name: string): number {
    return this.pending.filter((operation) => operation.name === name).length
  }

  /**
   * Wait until an operation of this name is in flight.
   *
   * Returns its variables so a test can assert on the request *before*
   * answering it -- which is the point at which "what cursor did we send"
   * is a question about the request rather than about the response.
   */
  async waitForRequest(name: string): Promise<Record<string, unknown>> {
    const found = await this.findPending(name)

    return found.variables
  }

  /** Answer the oldest in-flight request of this name. */
  async resolve(name: string, result: ApolloLink.Result): Promise<void> {
    const pending = await this.take(name)

    await act(async () => {
      pending.subscriber.next(result)
      pending.subscriber.complete()
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0)
      })
    })
  }

  /**
   * Fail the oldest in-flight request of this name at the transport.
   *
   * This is the channel a network outage arrives on, and it is a different
   * one from a GraphQL `errors` array -- which is an ordinary `resolve()`
   * with an `errors` key.
   */
  async fail(name: string, error: Error): Promise<void> {
    const pending = await this.take(name)

    await act(async () => {
      pending.subscriber.error(error)
      await new Promise<void>((resolve) => {
        setTimeout(resolve, 0)
      })
    })
  }

  /** Flush pending React work without answering anything. */
  async idle(): Promise<void> {
    await settle()
  }

  private async findPending(name: string): Promise<PendingOperation> {
    let found: PendingOperation | undefined

    await waitFor(() => {
      found = this.pending.find((operation) => operation.name === name)

      expect(
        found,
        `Expected a ${name} operation to be in flight. Dispatched so far: ` +
          `${this.operations.map((operation) => operation.name).join(', ') || '(none)'}`,
      ).toBeDefined()
    })

    if (found === undefined) {
      // Unreachable: `waitFor` above throws unless the expectation passed.
      // Written out anyway because narrowing a closure-assigned variable is
      // something TypeScript cannot do, and the alternative is a non-null
      // assertion that would also hide a real regression in `waitFor`.
      throw new Error(`No ${name} operation in flight`)
    }

    return found
  }

  private async take(name: string): Promise<PendingOperation> {
    const found = await this.findPending(name)
    const index = this.pending.indexOf(found)

    if (index !== -1) {
      this.pending.splice(index, 1)
    }

    return found
  }
}
