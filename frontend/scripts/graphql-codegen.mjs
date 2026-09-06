/**
 * Generate the TypeScript in `src/generated/` from `schema.graphql` and the
 * `.graphql` operation documents -- or fail if what is on disk is stale.
 *
 *     node scripts/graphql-codegen.mjs write
 *     node scripts/graphql-codegen.mjs check
 *
 * ## The two output files
 *
 *     src/generated/schema.ts       the schema as TypeScript
 *     src/generated/operations.ts   operation and fragment types, and a
 *                                   TypedDocumentNode per operation
 *
 * Both are written by every `write`, and both are compared by every `check`.
 * If you are here because a gate failed, those two are the paths it is
 * talking about, and they are the only two: there is no combined
 * single-file output, and nothing else under `src/generated/`.
 *
 * "Nothing else" is enforced, not merely asserted -- `check` also fails on
 * any file in those directories that codegen does not produce. See
 * `findOrphans` for why a comparison of the output set alone cannot catch a
 * generated file the generator has stopped generating.
 *
 * They are two files rather than one because `typescript` and
 * `typescript-operations` *both* emit the input types an operation's
 * variables mention: the first because it emits the whole schema, the second
 * because it has to be usable on its own. Run all three plugins into one
 * file and `IssueCreateInput` is declared twice in one module, which is
 * TS2300 and does not compile. `importSchemaTypesFrom` in ../codegen.ts is
 * the generator's own answer: `operations.ts` imports the shared input types
 * from `schema.ts` instead of restating them.
 *
 * ## Why a script instead of the `graphql-codegen` binary
 *
 * Two reasons, and neither is preference.
 *
 * 1. `@graphql-codegen/cli@7.4.0` has no check mode. Its flags are
 *    `--config --watch --require --overwrite --silent --errors-only
 *    --profile --project --verbose --debug --emit-legacy-common-js-imports
 *    --import-extension --ignore-no-documents`, and nothing there compares
 *    output against disk. `npm run graphql:check` had to be built, and a
 *    check built as "run the generator, then look at the result" is a check
 *    that passes whenever the generator succeeds -- which it does on stale
 *    input, because stale input is still valid input.
 * 2. The generator writes files itself, so a banner could not be part of the
 *    output without a fourth plugin. Here `write` and `check` call the same
 *    `build()` and differ only in what they do with the string it returns,
 *    which is the property that makes the check trustworthy: there is no
 *    second code path that could generate something slightly different.
 *
 * ## Why not `git diff --exit-code`
 *
 * That is the pattern the `locks` job in `.github/workflows/ci.yml` uses,
 * and it is right for the files it guards -- but it is only correct for
 * *tracked* files. `git diff` compares the working tree against the index;
 * an untracked file appears in neither, so a stale untracked
 * `src/generated/schema.ts` or `src/generated/operations.ts` produces an
 * empty diff and a green gate. That is precisely the state these files are
 * introduced in -- the whole of `frontend/` is untracked on the commit that
 * adds them. Comparing regenerated content against the bytes on disk gives
 * the same answer whether a file is tracked, staged, untracked or missing
 * entirely.
 *
 * A git-based gate can be made correct -- `git status --porcelain` does
 * report untracked files where `git diff` does not, and CI additionally
 * asserts each output is tracked before regenerating. The point is that the
 * correctness depends on picking the right git command, and this script
 * depends on nothing.
 *
 * ## Line endings
 *
 * `core.autocrlf` is true on the Windows machines this is developed on, so a
 * checkout hands this script CRLF where the generator produces LF. Both
 * sides are normalised to LF before comparison. A line ending is a property
 * of the checkout, not of the GraphQL contract, and a gate that failed on it
 * would be red on half the machines that ran it and would teach people to
 * ignore it.
 */

import { generate, loadContext } from '@graphql-codegen/cli'
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { dirname, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const FRONTEND_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const CONFIG_FILE = resolve(FRONTEND_DIR, 'codegen.ts')

/**
 * Prepended to every generated file.
 *
 * Not decoration. The first thing anyone does with a compiler error inside a
 * generated file is edit the generated file, and the edit survives until the
 * next `npm run graphql:codegen` silently reverts it. Saying where the
 * output came from, at the top, in the file itself, is the only place that
 * warning is read at the moment it is needed.
 */
const BANNER = `/**
 * GENERATED FILE -- DO NOT EDIT.
 *
 * Written by \`npm run graphql:codegen\` from:
 *   - frontend/schema.graphql                                (the backend's schema)
 *   - frontend/src/features/issues/api/operations.graphql    (the operations)
 *
 * Edit those, then regenerate. \`npm run graphql:check\` fails when this file
 * does not match them, so an edit made here does not survive review.
 */

`

/** LF, so a CRLF checkout is not mistaken for a stale generated file. */
function normalise(text) {
  return text.replace(/\r\n/g, '\n')
}

/**
 * The generated text exactly as it must appear in a committed file.
 *
 * `.pre-commit-config.yaml` runs `trailing-whitespace` and `end-of-file-fixer`
 * over everything outside `migrations/`, so both hooks *will* rewrite these
 * files after they are written. The drift gate then regenerates them and
 * diffs against the committed bytes. If this function did not do what the
 * hooks do, the two would disagree on the very first commit and the gate
 * would be red forever, on output nobody had touched -- the worst kind of
 * failing check, because the correct response to it is to ignore it.
 *
 * `typed-document-node` emits its last document without a trailing newline,
 * so the second normalisation is not hypothetical; it was the actual defect.
 * The first is insurance against a schema description that ends a line in
 * whitespace. Neither can corrupt the output: every embedded document is a
 * single-line JSON literal terminated by `;`, so nothing meaningful in this
 * file ever sits at a line end behind a space.
 */
function asCommitted(text) {
  return `${text
    .split('\n')
    .map((line) => line.replace(/[ \t]+$/, ''))
    .join('\n')
    .replace(/\n+$/, '')}\n`
}

/**
 * Every output file, as `{ path, content }`.
 *
 * `saveToFile: false` on purpose -- see the header. The generator computes
 * the content and this function owns what happens to it.
 */
async function build() {
  const context = await loadContext(CONFIG_FILE)
  const outputs = await generate(context, false)

  return outputs.map((output) => ({
    path: resolve(FRONTEND_DIR, output.filename),
    content: asCommitted(BANNER + output.content),
  }))
}

async function write() {
  for (const { path, content } of await build()) {
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, content, 'utf8')
    console.log(`Wrote ${relative(FRONTEND_DIR, path)} (${content.split('\n').length - 1} lines).`)
  }
}

/**
 * The first line at which two texts differ, 1-based, or null if they do not.
 */
function firstDifference(expected, actual) {
  const expectedLines = expected.split('\n')
  const actualLines = actual.split('\n')

  for (let index = 0; index < Math.max(expectedLines.length, actualLines.length); index += 1) {
    if (expectedLines[index] !== actualLines[index]) {
      return {
        line: index + 1,
        expected: expectedLines[index] ?? '<end of file>',
        actual: actualLines[index] ?? '<end of file>',
      }
    }
  }

  return null
}

/** Every file under `directory`, recursively, as absolute paths. */
function filesUnder(directory) {
  let entries

  try {
    entries = readdirSync(directory, { withFileTypes: true })
  } catch (reason) {
    // The directory does not exist yet, which is the state before the first
    // `write`. The missing *outputs* are reported by the comparison loop; an
    // absent directory holds no orphans.
    if (reason?.code === 'ENOENT') {
      return []
    }

    throw reason
  }

  return entries.flatMap((entry) => {
    const full = resolve(directory, entry.name)

    return entry.isDirectory() ? filesUnder(full) : [full]
  })
}

/**
 * Files sitting in the output directories that codegen does not produce.
 *
 * ## The failure this exists to catch
 *
 * Everything else in this script compares *the files the generator emits*
 * against disk. That is blind in one direction by construction: a file the
 * generator has stopped emitting is not in the output set, so it is never
 * looked at. It survives in the tree -- tracked, unchanged, importable, and
 * describing a schema that may no longer exist -- and CI does not see it
 * either, because `git status --porcelain` reports nothing about a tracked
 * file nobody modified. Both gates green, tree wrong.
 *
 * That is not hypothetical. `../codegen.ts` used to emit a single
 * `graphql.ts` and now emits `schema.ts` plus `operations.ts`. Had the old
 * file been committed before that change, it would still be here, still
 * importable, with every check passing. The only reason it is not is that
 * `frontend/` happened to be untracked at the time.
 *
 * Note that this is the *inverse* of the two traps already fixed in this
 * phase: an ignored `--out` flag and an `rm -f` on a nonexistent path both
 * failed because something was **absent**. This one fails because something
 * is **extra**, which is why neither of those fixes covers it.
 *
 * ## Why every file, not only `.ts`
 *
 * `src/generated/` is defined as exactly what the generator writes -- every
 * file in it carries a DO-NOT-EDIT banner this script prepends. So the
 * honest invariant is "this directory contains the output set and nothing
 * else", and checking it that way needs no list of extensions to keep in
 * step with whatever a future plugin emits. A file that genuinely belongs
 * next to generated output does not exist yet; if one ever does, this
 * failing is the conversation about where it should live.
 *
 * Nested directories are walked rather than reported wholesale, so a preset
 * that emits into a subdirectory is compared file by file like any other.
 */
function findOrphans(outputs) {
  const written = new Set(outputs.map((output) => output.path))
  const directories = new Set(outputs.map((output) => dirname(output.path)))

  return [...directories]
    .flatMap((directory) => filesUnder(directory))
    .filter((file) => !written.has(file))
    .sort()
}

async function check() {
  const outputs = await build()
  let stale = false

  for (const { path, content } of outputs) {
    const name = relative(FRONTEND_DIR, path)

    let onDisk

    try {
      onDisk = readFileSync(path, 'utf8')
    } catch (reason) {
      if (reason?.code !== 'ENOENT') {
        throw reason
      }

      console.error(`${name} has not been generated.`)
      stale = true
      continue
    }

    const difference = firstDifference(normalise(content), normalise(onDisk))

    if (difference === null) {
      console.log(`${name} is up to date.`)
      continue
    }

    console.error(`${name} is out of date with the schema or the operations.`)
    console.error(`First difference at line ${difference.line}:`)
    console.error(`  on disk:   ${JSON.stringify(difference.actual)}`)
    console.error(`  generated: ${JSON.stringify(difference.expected)}`)
    stale = true
  }

  const orphans = findOrphans(outputs)

  for (const orphan of orphans) {
    console.error(
      `${relative(FRONTEND_DIR, orphan)} is in a generated directory but codegen does not produce it.`,
    )
  }

  if (orphans.length > 0) {
    console.error(
      '\nThis is a file the generator used to emit and no longer does, or one\n' +
        'someone put there by hand. Either way it is stale generated code that\n' +
        'still compiles and can still be imported, and no other check can see\n' +
        'it. Delete it:\n' +
        orphans.map((orphan) => `  rm ${relative(FRONTEND_DIR, orphan)}`).join('\n'),
    )
  }

  if (stale) {
    console.error('\nRegenerate with `npm run graphql:codegen` and commit the result.')
  }

  if (stale || orphans.length > 0) {
    process.exitCode = 1
  }
}

const MODES = { write, check }
const mode = process.argv[2]

if (!Object.hasOwn(MODES, mode ?? '')) {
  console.error(`Usage: node scripts/graphql-codegen.mjs <${Object.keys(MODES).join('|')}>`)
  process.exit(2)
}

await MODES[mode]()
