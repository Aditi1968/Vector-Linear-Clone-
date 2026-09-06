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
    files: ['src/**/*.{ts,tsx}', '*.config.ts'],
    extends: [...tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
  },
)
