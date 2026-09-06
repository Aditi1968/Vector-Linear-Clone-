import { afterEach, describe, expect, it, vi } from 'vitest'

/**
 * Where the application sends GraphQL operations.
 *
 * ================================================================
 * Why this one constant is worth a test file
 * ================================================================
 *
 * `DEFAULT_GRAPHQL_URL` is a bare path, `/graphql`, and it must stay one.
 * Changing it to an absolute URL -- `http://127.0.0.1:8000/graphql`, the
 * address the backend actually listens on -- breaks the application in the
 * browser completely, and passes every other check in this repository.
 *
 * The failure: the backend installs no CORS middleware. A page served from
 * Vite's dev origin that requested an absolute backend URL would be making a
 * cross-origin request, and because Apollo sends
 * `content-type: application/json` that request is not "simple", so the
 * browser preflights it with OPTIONS. The backend answers the preflight
 * without `Access-Control-Allow-Origin`, and the browser discards the real
 * response before any JavaScript sees it. Every operation fails.
 *
 * What makes it worth pinning rather than trusting to review is that the
 * change looks like a *fix*. A bare path where a reader expects a URL reads
 * as an oversight; `curl http://127.0.0.1:8000/graphql` succeeds, because
 * `curl` does not enforce CORS; the dev server starts; the tests pass; the
 * build succeeds. The only place the absolute URL fails is a browser, which
 * is the only place that matters -- and someone debugging a connection
 * problem is exactly the person most likely to make the change.
 *
 * The same path is also correct in a deployed build, where a reverse proxy in
 * front of the API serves it, so there is no environment in which the
 * absolute form is the better default.
 *
 * The module reads `import.meta.env` at import time, so each case stubs the
 * variable and re-imports rather than mutating a value already resolved.
 */

afterEach(() => {
  vi.unstubAllEnvs()
})

async function resolveGraphqlUrl(configured: string | undefined): Promise<string> {
  vi.stubEnv('VITE_GRAPHQL_URL', configured)
  vi.resetModules()

  const { graphqlUrl } = await import('./env')

  return graphqlUrl
}

/** Anything of the form `scheme:` -- the shape that leaves this origin. */
const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:/i

function expectSameOrigin(url: string): void {
  // A path, and not a scheme-qualified URL.
  expect(HAS_SCHEME.test(url)).toBe(false)
  // Nor a protocol-relative URL, which is absolute despite starting with `/`.
  expect(url.startsWith('//')).toBe(false)
  expect(url.startsWith('/')).toBe(true)

  // And the whole point, stated the way the browser would decide it.
  expect(new URL(url, window.location.origin).origin).toBe(window.location.origin)
}

describe('graphqlUrl', () => {
  it('defaults to a same-origin path when nothing is configured', async () => {
    const url = await resolveGraphqlUrl(undefined)

    expect(url).toBe('/graphql')
    expectSameOrigin(url)
  })

  it.each([
    ['an empty string', ''],
    ['whitespace', '   '],
  ])('falls back to the same-origin default for %s', async (_label, configured) => {
    const url = await resolveGraphqlUrl(configured)

    expect(url).toBe('/graphql')
    expectSameOrigin(url)
  })

  it('uses a configured endpoint, trimmed', async () => {
    // So the tests above are pinning the default rather than asserting that
    // the module ignores its configuration.
    const url = await resolveGraphqlUrl('  /api/graphql  ')

    expect(url).toBe('/api/graphql')
    expectSameOrigin(url)
  })

  it('warns in development when a configured endpoint leaves this origin', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    const url = await resolveGraphqlUrl('http://127.0.0.1:8000/graphql')

    /*
      Two things at once.

      The behaviour: a developer who points the app at the backend's own
      origin gets told why it will not work *before* the network tab makes it
      look like the backend is down. A warning and not a throw, because this
      file cannot know whether the developer is pointing at something that
      does serve CORS headers.

      And the check on the check: `expectSameOrigin` must reject exactly this
      shape, or the tests above would pass against the very value they exist
      to forbid.
    */
    expect(url).toBe('http://127.0.0.1:8000/graphql')
    expect(HAS_SCHEME.test(url)).toBe(true)
    expect(new URL(url).origin).not.toBe(window.location.origin)

    expect(warn).toHaveBeenCalledWith(expect.stringContaining('CORS'))
  })
})
