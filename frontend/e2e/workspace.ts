import { readFileSync } from 'node:fs'

/**
 * The per-run workspace, written by `staging/auth.setup.ts` and read by every
 * staging spec. Each run provisions its own fresh user + workspace, so the
 * slug is dynamic (`vector-e2e-<epoch>`) rather than a shared constant — that
 * isolation is what lets the specs assert on their own freshly-created data in
 * an empty workspace instead of hunting through a shared, crowded one.
 */
const WORKSPACE_FILE = 'e2e/.auth/workspace.json'

export interface RunWorkspace {
  slug: string
  email: string
}

export function runWorkspace(): RunWorkspace {
  try {
    return JSON.parse(readFileSync(WORKSPACE_FILE, 'utf8')) as RunWorkspace
  } catch {
    // Before setup has run in this checkout (e.g. `playwright test --list`,
    // which imports specs without running the setup project) there is no file.
    // A placeholder keeps module evaluation from throwing; a real run always
    // writes the file before the staging workers import the specs.
    return { slug: 'e2e-pending', email: '' }
  }
}

export function workspaceSlug(): string {
  return runWorkspace().slug
}

export { WORKSPACE_FILE }
