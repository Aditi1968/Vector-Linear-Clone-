/**
 * The display and derivation rules the release screens share.
 *
 * Everything here is a pure function of what the server sent. Nothing invents
 * a value the API did not supply: a commit SHA is shortened but never
 * reformatted, an environment nobody can name is reported as such rather than
 * replaced by a plausible name, and a status with no legal move offers none.
 */

import type { BadgeTone } from '../../../components'
import type { Release, ReleaseStatus } from '../api'

/**
 * A deploy instant, to the minute, in UTC.
 *
 * Not `formatDay` from `features/projects/lib`, which is what every other
 * screen uses, and the reason is this screen specifically: the same version
 * legitimately ships twice in one day -- to staging and then to production, or
 * to production again after a rollback -- and two rows reading "7 Sep 2026"
 * would be indistinguishable statements about different deploys.
 *
 * UTC, matching `formatDay`, so a release note read in two offices names one
 * instant. An unparseable value comes back unchanged rather than as "Invalid
 * Date": the server sends ISO-8601 and anything else means something upstream
 * is wrong, which is worth seeing.
 */
const INSTANT_FORMAT = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'UTC',
})

export function formatInstant(value: string): string {
  const parsed = new Date(value)

  return Number.isNaN(parsed.getTime()) ? value : `${INSTANT_FORMAT.format(parsed)} UTC`
}

/**
 * The four release statuses, named and toned.
 *
 * A `Record` keyed by the generated union, so a status added to the schema is
 * a compile error here rather than a blank badge in production. The labels are
 * this interface's convention; the API exposes only `PENDING`, `DEPLOYED`,
 * `FAILED`, `ROLLED_BACK`.
 *
 * There is deliberately no "Deploying". Migration 024 declines to add the
 * state because nothing in this system observes a deploy in flight, and a
 * label for it here would be a state no writer ever produces.
 */
const STATUS_PRESENTATION: Record<ReleaseStatus, { label: string; tone: BadgeTone }> = {
  PENDING: { label: 'Pending', tone: 'neutral' },
  DEPLOYED: { label: 'Deployed', tone: 'success' },
  FAILED: { label: 'Failed', tone: 'danger' },
  ROLLED_BACK: { label: 'Rolled back', tone: 'warning' },
}

export function releaseStatusLabel(status: ReleaseStatus): string {
  return STATUS_PRESENTATION[status].label
}

export function releaseStatusTone(status: ReleaseStatus): BadgeTone {
  return STATUS_PRESENTATION[status].tone
}

/**
 * Which status may follow which.
 *
 * A copy of `RELEASE_TRANSITIONS` in `app/domain/releases.py`, and a copy is
 * what it has to be: the rule is not in the schema -- `ReleaseStatusSetInput`
 * takes any member of the enum -- so a menu that did not know it would offer
 * moves the server refuses, and a user would learn the rule by being told no.
 *
 * The copy is a convenience and NEVER the check. `ReleaseRepository.set_status`
 * applies the move in one statement naming the states it will move FROM, so
 * two people deploying the same pending release at once produce one winner and
 * one refusal, which no client-side table can predict. A rejection is rendered.
 *
 * `FAILED` and `ROLLED_BACK` are terminal, and that is a product decision
 * rather than an oversight: retrying a deploy is a NEW release, because a
 * second attempt that reused the first attempt's row would destroy the first
 * attempt's timestamp -- the one thing an incident review is looking for.
 */
const TRANSITIONS: Record<ReleaseStatus, readonly ReleaseStatus[]> = {
  PENDING: ['DEPLOYED', 'FAILED'],
  DEPLOYED: ['ROLLED_BACK'],
  FAILED: [],
  ROLLED_BACK: [],
}

export function nextStatuses(status: ReleaseStatus): readonly ReleaseStatus[] {
  return TRANSITIONS[status]
}

/**
 * How a move reads as a menu item.
 *
 * The verb, not the destination state: "Mark deployed" is an act somebody
 * performs, where "Deployed" beside three other nouns is a picker that looks
 * like it sets a field.
 */
const TRANSITION_LABELS: Record<ReleaseStatus, string> = {
  PENDING: 'Mark pending',
  DEPLOYED: 'Mark deployed',
  FAILED: 'Mark failed',
  ROLLED_BACK: 'Mark rolled back',
}

export function transitionLabel(status: ReleaseStatus): string {
  return TRANSITION_LABELS[status]
}

/**
 * The first seven characters of a commit SHA.
 *
 * What every Git interface shows and what a person recognises. The full forty
 * are kept in the `title` at the call site, because migration 024 stores the
 * full SHA for the reason `github_commits.sha` does -- the abbreviation is
 * ambiguous by construction, and this is display only.
 *
 * Anything that is not a full lowercase-hex SHA comes back UNCHANGED rather
 * than being truncated. The database refuses to store one, so a short string
 * here means something has gone wrong upstream, and silently trimming it to
 * seven characters would hide that.
 */
const SHA_PATTERN = /^[0-9a-f]{40}$/

export function shortSha(sha: string): string {
  return SHA_PATTERN.test(sha) ? sha.slice(0, 7) : sha
}

/**
 * The range a release's notes were generated from, as one string.
 *
 * `previousCommitSha` is null for the first release of a repository into an
 * environment, which migration 024 is explicit is a real state and not a
 * missing value: there is no lower bound, so the range is everything up to
 * `commitSha`. Rendered as "up to abc1234" rather than as a dash beside a
 * SHA, because a dash reads as a value that failed to load.
 */
export function commitRange(release: Pick<Release, 'commitSha' | 'previousCommitSha'>): string {
  const to = shortSha(release.commitSha)

  return release.previousCommitSha === null
    ? `Everything up to ${to}`
    : `${shortSha(release.previousCommitSha)} to ${to}`
}
