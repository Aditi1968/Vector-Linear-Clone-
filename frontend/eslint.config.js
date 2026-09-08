import tseslint from 'typescript-eslint'

/**
 * ESLint flat config.
 *
 * Type-checked rules rather than the syntactic-only preset. The syntactic
 * rules cannot see types, so the checks that matter most for a codebase with
 * a hard "no `any` as a shortcut" rule -- `no-unsafe-assignment`,
 * `no-unsafe-member-access`, `no-unsafe-return` -- are exactly the ones they
 * cannot run. Those are what catch `any` arriving from an untyped library
 * boundary and spreading, which is how `any` actually enters a strict
 * codebase: not by being written, but by being inherited and never annotated.
 *
 * The cost is that linting needs a TypeScript program, so a linted file has
 * to belong to a tsconfig. `projectService` resolves that per file from
 * tsconfig.app.json and tsconfig.node.json.
 *
 * Not configured here, and worth the Lead deciding on: `eslint-plugin-react-
 * hooks` (rules-of-hooks and exhaustive-deps) is not in the approved
 * dependency list, so a mis-ordered hook or a stale effect dependency is not
 * caught by this config. That is the main gap in the current rule set.
 */
export default tseslint.config(
  {
    ignores: ['dist/**', 'node_modules/**', 'coverage/**'],
  },
  {
    // eslint.config.js itself is deliberately absent: it is plain JavaScript,
    // belongs to no tsconfig, and type-checked rules cannot be applied to it.
    //
    // The two build configs are named rather than matched with `*.config.ts`,
    // and the list is exactly tsconfig.node.json's `include`. That is not a
    // coincidence to be tidied away: `projectService` resolves every linted
    // file to a TypeScript project, and a file that belongs to none is a hard
    // parsing error rather than a skipped file. A glob is therefore a promise
    // that every root config is in a tsconfig -- and playwright.config.ts is
    // deliberately in none of them, along with the e2e/ suite it configures.
    // Neither is compiled by `tsc -b` or bundled by vite; Playwright
    // transpiles them itself, and the `e2e` job in .github/workflows/ci.yml
    // is what proves they are correct, by running them.
    //
    // ponytail: the e2e suite is unlinted and untyped as a result. Giving it
    // a tsconfig of its own means `@types/node` as a real devDependency and a
    // project reference for the one `src/` module it imports; worth doing the
    // day a spec is big enough that a type error in it is not obvious.
    files: ['src/**/*.{ts,tsx}', 'vite.config.ts', 'vitest.config.ts'],
    extends: [...tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
)
