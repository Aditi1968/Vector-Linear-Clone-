/// <reference types="vite/client" />

/**
 * Typing for this application's own environment variables.
 *
 * `vite/client` declares `ImportMetaEnv` with an `[key: string]: any` index
 * signature, which means `import.meta.env.VITE_ANYTHING` type-checks as `any`
 * -- including a variable that does not exist and a typo of one that does.
 * Declaration merging adds an explicit property, and an explicit property
 * beats an index signature, so the value below narrows to `string | undefined`
 * and the `any` never reaches application code.
 *
 * A new `VITE_` variable belongs here as well as in .env.example.
 */
interface ImportMetaEnv {
  /** Where the browser sends GraphQL operations. See ./env.ts. */
  readonly VITE_GRAPHQL_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
