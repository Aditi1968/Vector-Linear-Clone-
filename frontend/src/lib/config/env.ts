/**
 * The one place that decides where the API lives.
 *
 * Nothing else in the application may build a GraphQL URL. Every consumer
 * goes through `graphqlUrl`, so changing the endpoint -- or discovering, in
 * a later phase, that it needs a tenant segment or a version prefix -- is a
 * change to this file and to nothing else.
 */

/**
 * Same-origin by default, and a *path* rather than an absolute URL.
 *
 * This is what makes the Vite dev proxy work (see vite.config.ts): the
 * browser asks its own origin for `/graphql`, the dev server forwards it to
 * the backend, and no cross-origin request is ever made -- which matters
 * because the backend serves no CORS headers. In a deployed build the same
 * path is served by whatever reverse proxy sits in front of the API, so the
 * default is correct in both environments and no production host is baked
 * into the bundle.
 */
const DEFAULT_GRAPHQL_URL = '/graphql'

function resolveGraphqlUrl(): string {
  const configured = import.meta.env.VITE_GRAPHQL_URL?.trim()

  if (configured === undefined || configured.length === 0) {
    return DEFAULT_GRAPHQL_URL
  }

  return configured
}

/**
 * Whether a configured endpoint would leave this page's origin.
 *
 * A relative path never does. An absolute URL does whenever its origin
 * differs from the page's -- which is the misconfiguration worth catching,
 * because its symptom (every operation failing in the browser, while `curl`
 * against the same URL succeeds) does not look like a configuration problem.
 */
function isCrossOrigin(url: string): boolean {
  try {
    return new URL(url, window.location.origin).origin !== window.location.origin
  } catch {
    // An unparseable value is not a cross-origin value; let the request fail
    // on its own terms rather than reporting the wrong diagnosis.
    return false
  }
}

export const graphqlUrl: string = resolveGraphqlUrl()

if (import.meta.env.DEV && typeof window !== 'undefined' && isCrossOrigin(graphqlUrl)) {
  // Development only, and a warning rather than a throw: a developer may
  // genuinely be pointing at a backend that does serve CORS headers, and
  // this file has no way to know. What it can do is name the cause before
  // the network tab makes it look like the backend is down.
  console.warn(
    `[vector] VITE_GRAPHQL_URL is set to "${graphqlUrl}", which is a different ` +
      `origin from ${window.location.origin}. The Vector backend installs no CORS ` +
      `middleware, so the browser will discard these responses even though the ` +
      `backend answers them. Prefer a same-origin path such as "${DEFAULT_GRAPHQL_URL}" ` +
      `and let the Vite dev proxy forward it.`,
  )
}
