/**
 * Export the backend's GraphQL schema to `frontend/schema.graphql`, or fail
 * if the checked-in copy no longer matches it.
 *
 *     node scripts/graphql-schema.mjs write
 *     node scripts/graphql-schema.mjs check
 *
 * ## Why a Node wrapper around a Python script
 *
 * The SDL comes from `scripts/export_schema.py`, which imports the real
 * Strawberry schema. Python puts the *script's own directory* on `sys.path`,
 * not the working directory, so `python frontend/scripts/export_schema.py`
 * cannot import `app` no matter where it is run from. The import path has to
 * be supplied from outside, and the two portable ways to supply it -- a
 * working directory and `PYTHONPATH` -- are exactly what a spawn gives and
 * what an npm script cannot express: `PYTHONPATH=.. python ...` is shell
 * syntax that cmd.exe does not understand, and npm runs scripts through
 * cmd.exe on Windows.
 *
 * ## Why this is a check and not `git diff`
 *
 * A regenerate-then-`git diff --exit-code` gate is the pattern used for the
 * dependency locks, and it works there because those files are tracked. It
 * is not usable for a check that has to be correct on the commit that
 * *introduces* the file: `git diff` compares the working tree against the
 * index, and an untracked file is in neither, so a stale untracked
 * `schema.graphql` would sail through a diff gate reporting no changes.
 * Comparing regenerated content against the file's own bytes has no such
 * blind spot -- it is the same answer whether the file is tracked, staged,
 * untracked or absent.
 *
 * ## Line endings
 *
 * `core.autocrlf` is true on the Windows machines this is developed on, so a
 * checkout can hand this script CRLF where the exporter produces LF. Both
 * sides are normalised to LF before comparison: a line ending is a property
 * of the checkout, not of the schema, and reporting it as schema drift would
 * make the gate cry wolf on half the machines that run it.
 */

import { spawnSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { delimiter, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const FRONTEND_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const REPO_ROOT = resolve(FRONTEND_DIR, '..')
const EXPORTER = join(FRONTEND_DIR, 'scripts', 'export_schema.py')
const SCHEMA_FILE = join(FRONTEND_DIR, 'schema.graphql')

/**
 * Interpreters to try, in order.
 *
 * `PYTHON` first so a project with a virtualenv that is not on PATH has a
 * way in without editing this file. `python3` is tried after `python`
 * because on Windows `python3` is frequently a Microsoft Store stub that
 * exits non-zero rather than an interpreter.
 */
const INTERPRETERS = [process.env.PYTHON, 'python', 'python3'].filter(Boolean)

/** LF, so that a CRLF checkout is not mistaken for a changed schema. */
function normalise(text) {
  return text.replace(/\r\n/g, '\n')
}

/**
 * The SDL, straight from the Strawberry schema.
 *
 * Fails loudly rather than falling back to the checked-in file. A gate that
 * passes because the tool it needed was missing is worse than no gate: it
 * reports "no drift" about a comparison it never made.
 */
function exportSdl() {
  const attempts = []

  for (const interpreter of INTERPRETERS) {
    const result = spawnSync(interpreter, [EXPORTER], {
      cwd: REPO_ROOT,
      // `import app` resolves against this and nothing else -- see the note
      // at the top. Prepended rather than replacing, so an interpreter that
      // needs its own PYTHONPATH entries keeps them.
      env: {
        ...process.env,
        PYTHONPATH: [REPO_ROOT, process.env.PYTHONPATH]
          .filter(Boolean)
          .join(delimiter),
      },
      // Buffers, not strings: Node decodes with the platform encoding
      // otherwise, and the point of the exporter writing raw bytes is that
      // nothing between it and here rewrites a newline.
      encoding: 'buffer',
      windowsHide: true,
    })

    if (result.error?.code === 'ENOENT') {
      attempts.push(`${interpreter}: not found`)
      continue
    }

    if (result.error) {
      attempts.push(`${interpreter}: ${result.error.message}`)
      continue
    }

    if (result.status !== 0) {
      const stderr = result.stderr?.toString('utf8').trim() ?? ''
      // A non-zero exit from an interpreter that *ran* is a real failure --
      // a syntax error in the backend, a missing dependency, a schema that
      // no longer builds. Reported immediately rather than being retried
      // against the next interpreter, which would bury the message.
      throw new Error(
        `${interpreter} failed to export the schema (exit ${result.status}).\n${stderr}`,
      )
    }

    return result.stdout.toString('utf8')
  }

  throw new Error(
    'No Python interpreter could export the GraphQL schema.\n' +
      `${attempts.join('\n')}\n` +
      'Set PYTHON to the interpreter that has the backend dependencies installed.',
  )
}

function write() {
  const sdl = exportSdl()

  writeFileSync(SCHEMA_FILE, sdl, 'utf8')
  console.log(`Wrote ${SCHEMA_FILE} (${sdl.split('\n').length - 1} lines).`)
}

function check() {
  const expected = normalise(exportSdl())

  let actual

  try {
    actual = normalise(readFileSync(SCHEMA_FILE, 'utf8'))
  } catch (reason) {
    if (reason?.code === 'ENOENT') {
      console.error(
        `frontend/schema.graphql does not exist. Run \`npm run graphql:schema\`.`,
      )
      process.exitCode = 1
      return
    }

    throw reason
  }

  if (actual === expected) {
    console.log('frontend/schema.graphql matches the backend schema.')
    return
  }

  console.error(
    'frontend/schema.graphql is out of date with the backend GraphQL schema.\n' +
      'Regenerate it and the types built from it:\n' +
      '  npm run graphql:schema\n' +
      '  npm run graphql:codegen\n',
  )

  // The first differing line, because "the file changed" is not actionable
  // and printing two whole schemas is not either.
  const expectedLines = expected.split('\n')
  const actualLines = actual.split('\n')

  for (let index = 0; index < Math.max(expectedLines.length, actualLines.length); index += 1) {
    if (expectedLines[index] !== actualLines[index]) {
      console.error(`First difference at line ${index + 1}:`)
      console.error(`  checked in: ${JSON.stringify(actualLines[index] ?? '<end of file>')}`)
      console.error(`  backend:    ${JSON.stringify(expectedLines[index] ?? '<end of file>')}`)
      break
    }
  }

  process.exitCode = 1
}

const MODES = { write, check }
const mode = process.argv[2]

if (!Object.hasOwn(MODES, mode ?? '')) {
  console.error(`Usage: node scripts/graphql-schema.mjs <${Object.keys(MODES).join('|')}>`)
  process.exit(2)
}

try {
  MODES[mode]()
} catch (reason) {
  // The message, not the stack. Everything thrown above is a diagnosis --
  // "no interpreter", "the schema no longer builds, here is Python's own
  // traceback" -- and a Node stack on top of it only buries the sentence
  // the reader needs. The non-zero exit is what matters, and it is kept:
  // a gate that cannot run has to look exactly like a gate that failed.
  console.error(reason instanceof Error ? reason.message : String(reason))
  process.exit(1)
}
