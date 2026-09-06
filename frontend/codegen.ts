/**
 * GraphQL Code Generator configuration.
 *
 * Read by `scripts/graphql-codegen.mjs`, which is what both
 * `npm run graphql:codegen` and `npm run graphql:check` go through. The
 * script -- not this file -- decides whether the result is written or
 * compared, so the two commands compute byte-identical output by
 * construction rather than by two configurations agreeing.
 *
 * ## The schema is a file, never a URL
 *
 * `schema.graphql` is exported from the real Strawberry schema by
 * `npm run graphql:schema`, and `npm run graphql:schema:check` fails when it
 * has fallen behind. Pointing this at `http://127.0.0.1:8000/graphql`
 * instead would make type generation depend on a running backend, and would
 * make it silently *impossible* in production, where the schema builds with
 * `DisableIntrospection`. A checked-in SDL also makes the diff that changes
 * the contract visible in review, which an introspection fetch never is.
 *
 * ## Why no preset
 *
 * `client-preset` is the modern default and it is the wrong shape here. It
 * generates a `graphql()` function that parses documents from template
 * literals inside `.ts` files, which puts operation text back into
 * TypeScript -- the thing moving to `operations.graphql` was meant to
 * undo -- and it emits a multi-file `gql/` directory whose fragment masking
 * would change how `useIssueList` and `prependCreatedIssue` read their own
 * results. Three plugins across the two files below produce exactly what
 * this codebase already consumes: schema types, per-operation result and
 * variable types, and a `TypedDocumentNode` per operation.
 *
 * ## This file is not type-checked
 *
 * `tsc -b` covers `src/` (tsconfig.app.json) and the two Vite configs
 * (tsconfig.node.json), and this file is in neither, so the `CodegenConfig`
 * annotation below is an editor aid rather than a gate. Adding it to
 * tsconfig.node.json's `include` would make it a real one -- that file
 * belongs to another owner, so it is flagged here rather than changed.
 */

import type { CodegenConfig } from '@graphql-codegen/cli'

/**
 * Configuration both generated files share.
 *
 * Shared rather than repeated because a scalar mapping that applied to one
 * file and not the other would produce two incompatible spellings of the
 * same field -- `Scalars['UUID']['output']` here, `any` there -- and the
 * mismatch would surface as an assignability error a long way from its
 * cause.
 */
const SHARED = {
  /*
   * The three custom scalars this schema declares, mapped to what the
   * transport actually delivers: a hyphenated UUID string, an ISO-8601
   * datetime string, and -- since projects -- an ISO-8601 calendar date,
   * `YYYY-MM-DD`.
   *
   * `Date` and `DateTime` are different scalars carrying different strings,
   * and the backend chose the distinction deliberately: a project's target
   * date is a day people in several timezones agree on, so it is a `DATE`
   * column rather than an instant. Both map to `string` here, so the
   * difference is documentation on this side rather than a type -- a
   * date-only string handed to `new Date()` is parsed as UTC midnight, which
   * is the one place it matters.
   *
   * None of them is parsed into a richer type at this boundary on purpose.
   * Apollo stores what it is given, and a `Date` object in the cache would be
   * reconstructed on every read and defeat the cache's structural equality
   * checks. Parsing happens where a date is formatted
   * (`src/features/issues/lib/dates.ts`).
   */
  scalars: {
    UUID: 'string',
    DateTime: 'string',
    Date: 'string',
  },

  /*
   * An unmapped custom scalar fails generation instead of becoming `any`.
   *
   * This is the setting that keeps the next scalar the backend adds from
   * arriving in this codebase as an untyped hole that every type-checked
   * lint rule then has to be argued with.
   */
  strictScalars: true,

  /*
   * GraphQL enums become string-literal unions, not TypeScript `enum`s.
   *
   * Not a preference: `erasableSyntaxOnly` is on in both tsconfigs, and a
   * TypeScript `enum` is the canonical thing it forbids -- it is the one
   * declaration that emits runtime code, so the generated file stopped
   * compiling (TS1294) the moment the schema declared its first enum. A
   * union of the literal values is erasable, ships nothing at runtime, and is
   * the shape the data actually arrives in: the transport delivers
   * `"BACKLOG"`, so a union can be compared and switched on directly rather
   * than through an imported member.
   *
   * `enumsAsConst` is the other erasable option and would additionally give
   * an importable value object. It is not used because nothing here needs
   * one; a `Record<WorkflowStateCategory, T>` keyed by the literals is
   * exhaustively checked either way.
   */
  enumsAsTypes: true,

  /*
   * `import type`, because `verbatimModuleSyntax` is on.
   *
   * Without it the generated files emit *value* imports for things that are
   * only types. `verbatimModuleSyntax` preserves such an import verbatim
   * into the emitted module, and the bundler then fails to resolve an export
   * that never existed outside the type system.
   */
  useTypeImports: true,

  /*
   * A string-literal union per GraphQL enum, because `erasableSyntaxOnly`
   * is on in tsconfig.app.json.
   *
   * The default emits a TypeScript `enum`, which is one of the constructs
   * that setting forbids: an `enum` is not type-only syntax, it compiles to
   * a runtime object, and `tsc -b` refuses it with TS1294. The first enum
   * the backend declared (`WorkspaceRole`) is what surfaced this, and it
   * failed generation-then-typecheck rather than at generation time, so the
   * setting is recorded here next to the reason.
   *
   * `enumsAsConst` would satisfy the same rule and is not chosen: it emits
   * a value the bundler has to keep, to describe a set that only ever
   * arrives as a string on the wire. A union costs nothing at runtime and
   * narrows in a `switch` exactly as an enum does.
   */
  enumsAsTypes: true,
}

const config: CodegenConfig = {
  // The SDL exported from the backend. Regenerate with `npm run graphql:schema`.
  schema: './schema.graphql',

  /*
   * Operation documents.
   *
   * Restricted to `src/` so that `schema.graphql` at the project root can
   * never be picked up as a document -- a schema loaded as a document is not
   * an error, it is an empty document set plus a confusing type file.
   */
  documents: ['src/**/*.graphql'],

  /*
   * A missing operation document is an error.
   *
   * The alternative (`ignoreNoDocuments: true`) is how a renamed or deleted
   * `.graphql` file turns into a generated file that quietly loses every
   * operation type, and then into a wave of "does not exist on type" errors
   * with no obvious cause.
   */
  ignoreNoDocuments: false,

  generates: {
    /*
     * The schema, as TypeScript: `Issue`, `PageInfo`, `IssueCreateInput`,
     * `IssueCreatePayload`, the `Query`/`Mutation` roots and their argument
     * types, plus the `Scalars` map every one of them is written against.
     *
     * These describe what the server *declares*. What an operation
     * *selects* is a different and usually narrower shape, and lives in
     * ./operations.
     */
    'src/generated/schema.ts': {
      plugins: ['typescript'],
      config: SHARED,
    },

    /*
     * The operations: a result type and a variables type per operation, the
     * fragment types they are built from, and a `TypedDocumentNode` per
     * operation carrying both as parameters -- which is what makes
     * `useQuery(IssueListDocument)` infer its data and its variables with no
     * annotation at the call site.
     *
     * ## Why this is a second file rather than three plugins in one
     *
     * Not taste, and not a preference for small files. `typescript` and
     * `typescript-operations` both emit the *input* types an operation's
     * variables mention -- `typescript` because it emits the whole schema,
     * `typescript-operations` because it must be usable on its own. Put both
     * in one file and `IssueCreateInput` is declared twice in one module,
     * which is TS2300 and does not compile.
     *
     * `importSchemaTypesFrom` is the mechanism the generator provides for
     * exactly this: the operations file imports the shared input and enum
     * types instead of restating them, so there is one declaration of
     * `IssueCreateInput` in the codebase and the operations file references
     * it as `Types.IssueCreateInput`.
     */
    'src/generated/operations.ts': {
      plugins: ['typescript-operations', 'typed-document-node'],

      config: {
        ...SHARED,

        /*
         * Where the shared schema types come from. Resolved relative to the
         * project root (the generator's working directory), then rewritten
         * as a path relative to this output file.
         */
        importSchemaTypesFrom: './src/generated/schema',

        /*
         * No extension on that import.
         *
         * The default appends `.js`, which is correct for a project emitting
         * ESM to disk and wrong for this one: nothing here is emitted --
         * `noEmit` is set and Vite resolves the TypeScript source directly.
         */
        importExtension: '',

        /*
         * Take `TypedDocumentNode` from Apollo rather than from
         * `@graphql-typed-document-node/core`.
         *
         * It is the same type -- Apollo re-exports it -- but `@apollo/client`
         * is already a direct dependency of this package, and the core
         * package is only ever present as somebody else's transitive one.
         * Importing it directly would be depending on a package this project
         * does not declare, which works until a lockfile refresh flattens it
         * somewhere else.
         */
        documentNodeImport: '@apollo/client#TypedDocumentNode',

        /*
         * `__typename` is present on every selected object, and required.
         *
         * The generator's default is to type it only where a document asks
         * for it by name, and none of these do. That default is wrong for an
         * Apollo client: `InMemoryCache` rewrites every outgoing document to
         * add `__typename` to every selection set -- that is how it
         * normalises entities -- so the field is always in the response, and
         * a type that omits it makes writing a correct fixture a type error
         * (`src/test/factories.ts` builds every response with it).
         *
         * Required rather than optional because it is not conditionally
         * present. This is also what the hand-written types it replaces
         * declared.
         */
        nonOptionalTypename: true,

        /*
         * ...except on `Query` and `Mutation` themselves.
         *
         * Apollo does not add `__typename` to the root selection set and the
         * server does not return one unless asked, so requiring it there
         * would demand a field that never arrives.
         */
        skipTypeNameForRoot: true,

        /*
         * One definition per fragment in the emitted documents.
         *
         * `IssueDetailFields` spreads `IssueRowFields`, and the create
         * mutation spreads `IssueDetailFields`, so without this the row
         * fragment would be embedded twice in the mutation's AST. GraphQL
         * rejects a document that defines one fragment name twice.
         */
        dedupeFragments: true,
      },
    },
  },
}

export default config
