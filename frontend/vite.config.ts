import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Origin of the Python backend during local development.
 *
 * `127.0.0.1` rather than `localhost` on purpose. Node resolves `localhost`
 * through the OS resolver, which on Windows and on recent Node returns `::1`
 * (IPv6) first, while `uvicorn` binds `127.0.0.1` (IPv4 only) by default. A
 * proxy pointed at `localhost` therefore fails with ECONNREFUSED against a
 * backend that is demonstrably running -- a confusing failure with an
 * uninteresting cause.
 */
const BACKEND_ORIGIN = 'http://127.0.0.1:8000'

/**
 * The path the browser asks for, and the path the backend serves.
 *
 * These are deliberately the same string, which is what makes the proxy a
 * pure transport concern: the app's request URL is `/graphql` whether or not
 * a proxy is in front of it. See `src/lib/config/env.ts`.
 */
const GRAPHQL_PATH = '/graphql'

/**
 * The provider flows, which are browser redirects rather than fetches.
 *
 * "Connect GitHub" is an ordinary link to a backend route that answers 302
 * to github.com. Served from Vite without a proxy entry, that link matched
 * the SPA fallback instead: the browser got `index.html` with status 200,
 * the router rendered not-found, and nothing anywhere reported an error.
 * The integration was unreachable from the running application while every
 * test stayed green, because no test goes through the dev server.
 *
 * `/integrations` is the prefix the deployment's GitHub App and Smee relay
 * are configured against; the two bare prefixes are the older spellings the
 * same routers still answer on.
 */
const PROVIDER_PATHS = ['/integrations', '/github', '/slack']

/**
 * The proxy table, shared by `vite` and `vite preview`.
 *
 * Declared once and used twice because Vite reads `server.proxy` and
 * `preview.proxy` as two independent options: neither inherits from the
 * other, and a table given only to `server` leaves `npm run preview` -- the
 * only way to look at the real production bundle locally -- as the one mode
 * where every GraphQL response is silently discarded by CORS. That failure
 * looks like a broken build rather than a missing proxy, which is what makes
 * one definition worth more than the second copy it prevents.
 */
const proxy = {
  /**
   * LOAD-BEARING. This is not a convenience, and removing it breaks the
   * application in the browser -- while leaving every test green.
   *
   * The backend installs no CORS middleware (verified: there is no
   * `CORSMiddleware` anywhere under `app/`). A page served from Vite's
   * dev origin (`http://localhost:5173`) that called
   * `http://127.0.0.1:8000/graphql` directly would be making a
   * cross-origin request. Because Apollo sends
   * `content-type: application/json`, that request is not "simple", so
   * the browser preflights it with OPTIONS -- the backend answers the
   * preflight without `Access-Control-Allow-Origin`, and the browser
   * discards the real response before any JavaScript sees it. `curl`
   * against the same URL succeeds throughout, because `curl` does not
   * enforce CORS. That asymmetry is what makes this worth a comment.
   *
   * Adding CORS to the backend is the other way to fix it, and is not
   * available: the backend is frozen for this phase. Proxying costs no
   * backend change and has a second benefit that outlives the freeze --
   * the app talks to a same-origin URL, so cookie-based auth (Phase
   * 1b-5 and later) needs no `SameSite=None`, no credentialed-CORS
   * allow-list, and no origin echoing.
   *
   * Only `/graphql` is proxied. `/healthz` and `/readyz` are deliberately
   * left off: the product never calls them, and proxying the whole
   * backend would quietly expose every future endpoint to the dev origin.
   */
  [GRAPHQL_PATH]: {
    target: BACKEND_ORIGIN,
    // The backend does not inspect Host, and preserving it keeps dev
    // logs honest about where the request claimed to be going.
    changeOrigin: false,
  },

  // Same target and the same Host handling. `changeOrigin: false` matters
  // more here than for `/graphql`: these routes set and read the OAuth state
  // cookie, and a rewritten Host is how a cookie ends up scoped to somewhere
  // the next request will not send it back from.
  ...Object.fromEntries(
    PROVIDER_PATHS.map((path) => [path, { target: BACKEND_ORIGIN, changeOrigin: false }]),
  ),
}

/**
 * Hosts the dev server will answer to besides localhost.
 *
 * Vite refuses a request whose Host it does not recognise, which is a DNS
 * rebinding protection and worth keeping. But a provider OAuth callback has
 * to arrive on a public HTTPS origin -- Slack will not register an http
 * callback at all -- so during development the app is fronted by a tunnel,
 * and every request through it carries the tunnel's Host.
 *
 * A suffix rather than one URL: a free tunnel's subdomain changes on every
 * restart, and pinning the current one would mean editing this file each
 * time. Development only; `vite build` output is served by something else
 * entirely and this setting has no part in it.
 */
const DEV_TUNNEL_HOSTS = ['.ngrok-free.app', '.ngrok.io', '.trycloudflare.com']

export default defineConfig({
  plugins: [react()],
  server: { proxy, allowedHosts: DEV_TUNNEL_HOSTS },
  preview: { proxy, allowedHosts: DEV_TUNNEL_HOSTS },
})
