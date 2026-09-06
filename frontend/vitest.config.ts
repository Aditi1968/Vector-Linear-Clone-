import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/**
 * Vitest's own config, deliberately separate from vite.config.ts.
 *
 * Vitest prefers `vitest.config.*` over `vite.config.*` and does not merge
 * the two, so the React plugin is repeated here rather than inherited. That
 * is the intended trade: vite.config.ts carries the dev proxy, which is a
 * statement about how a *browser* reaches the backend and has no meaning in
 * a jsdom process. A test run that loaded it would be configuring a proxy no
 * test can use, and -- more to the point -- a change to the proxy could then
 * change how tests behave.
 *
 * `globals` is left off. The alternative would require `"vitest/globals"` in
 * tsconfig.app.json's `types`, which belongs to another teammate; importing
 * `describe`/`it`/`expect` from `vitest` explicitly needs no config change at
 * all and makes each test file say where its assertions come from.
 *
 * `css` is left off too. Every component here imports a CSS module, and
 * Vitest replaces those imports with a proxy rather than compiling them.
 * Compiling them would cost real time per file to produce class names that no
 * assertion in this suite reads -- the tests query by role and accessible
 * name, never by class -- so the proxy loses nothing.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    // Each file gets a fresh module registry, so the module-scope Apollo
    // client in src/lib/graphql/client.ts cannot carry a cache between files.
    // Tests build their own client regardless; this makes it structural.
    isolate: true,
    restoreMocks: true,
  },
})
