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

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
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
    },
  },
})
