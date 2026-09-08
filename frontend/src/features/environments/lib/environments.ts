/**
 * The display rules the environment screens share.
 *
 * Two screens read them: this feature's own list, and the releases list, where
 * a release row names the target it went to.
 */

import type { BadgeTone } from '../../../components'
import type { Environment, EnvironmentKind } from '../api'

/**
 * The four kinds, named and toned.
 *
 * A `Record` keyed by the generated union, so a kind added to the schema is a
 * compile error here rather than a blank badge in production.
 *
 * `kind` is stored beside `name` rather than derived from it, and migration
 * 024 says why: a workspace may have several production environments ("Prod
 * EU", "Prod US"), and a rule written against the string "production" would
 * recognise neither. The kind is what a policy keys off and it survives a
 * rename, so it is what this badge shows -- the name is already the row's
 * title beside it.
 *
 * Production is toned `danger` and that is not an error state: it is the one
 * kind where a deploy is irreversible in the way that matters, and the design
 * has no fifth tone that means "handle with care".
 */
const KIND_PRESENTATION: Record<EnvironmentKind, { label: string; tone: BadgeTone }> = {
  DEVELOPMENT: { label: 'Development', tone: 'neutral' },
  STAGING: { label: 'Staging', tone: 'info' },
  PRODUCTION: { label: 'Production', tone: 'danger' },
  CUSTOM: { label: 'Custom', tone: 'neutral' },
}

/**
 * Every kind, in the order the composer should offer them.
 *
 * Development first because it is the safest default for somebody adding a
 * target for the first time, and `CUSTOM` last because it is the answer you
 * reach for once the other three have been rejected -- a preview environment
 * per pull request, a load-test rig, a customer sandbox. Migration 024 is
 * explicit that it is a real member and not an escape hatch.
 */
export const ENVIRONMENT_KINDS: readonly EnvironmentKind[] = [
  'DEVELOPMENT',
  'STAGING',
  'PRODUCTION',
  'CUSTOM',
]

export function environmentKindLabel(kind: EnvironmentKind): string {
  return KIND_PRESENTATION[kind].label
}

export function environmentKindTone(kind: EnvironmentKind): BadgeTone {
  return KIND_PRESENTATION[kind].tone
}

/**
 * `Release.environmentId` -> the environment it names.
 *
 * The schema exposes no `Release.environment`, only the raw id, so every
 * screen that wants to name a release's target resolves it through this. An
 * id with no match comes back undefined and the caller says so; it is not
 * replaced by a plausible name, because an environment the viewer cannot see
 * and one that was deleted are the same answer here and neither is "Staging".
 */
export function environmentsById(
  environments: readonly Environment[],
): ReadonlyMap<string, Environment> {
  return new Map(environments.map((environment) => [environment.id, environment]))
}
