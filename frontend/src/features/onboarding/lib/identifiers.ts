/**
 * The two identifiers first-run setup asks a person to choose, and the rules
 * the server will actually judge them by.
 *
 * Both are permanent in the way that matters: a workspace slug is in every
 * URL, and a team key is the prefix of every issue identifier that team will
 * ever mint. Getting them wrong is not a form error, it is a migration. So
 * the rules are surfaced as the person types rather than after they submit.
 *
 * ## These rules are a mirror, not the authority
 *
 * The server validates independently and stays the only authority --
 * `WorkspaceCreateInput.slug` and `TeamCreateInput.key` carry the canonical
 * statements of these rules in `schema.graphql`, quoted verbatim in the
 * comments below, and every message the forms show comes from the payload's
 * `errors` when a submit is rejected. What is here decides only when a hint
 * turns from grey to red before a submit happens.
 *
 * Which is also why neither input gets a `pattern` or a `maxLength`
 * attribute. A native constraint makes the server's rule unreachable: the
 * browser refuses the value, the request is never sent, and the form is
 * silently enforcing a rule it merely *believes* in -- so the day the server
 * relaxes or tightens it, the client keeps enforcing the old one and nobody
 * finds out.
 */

/**
 * `schema.graphql`, on `WorkspaceCreateInput.slug`: "The workspace's URL
 * segment. Lowercase letters, digits and hyphens; must start and end with a
 * letter or digit."
 *
 * A single character is therefore legal as long as it is alphanumeric, which
 * is why the pattern's middle group is optional rather than `+`.
 */
const SLUG_PATTERN = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/

/** Said the way a person reads it, not the way a regex reads it. */
export const SLUG_RULE =
  'Lowercase letters, digits and hyphens. Must start and end with a letter or digit.'

/**
 * `schema.graphql`, on `TeamCreateInput.key`: "The prefix of this team's
 * issue identifiers -- the ENG in ENG-42. 1-10 uppercase letters and digits,
 * starting with a letter. Unique within the workspace."
 */
const TEAM_KEY_PATTERN = /^[A-Z][A-Z0-9]{0,9}$/

export const TEAM_KEY_MAX_LENGTH = 10

export const TEAM_KEY_RULE =
  'Up to 10 uppercase letters and digits, starting with a letter. No hyphens.'

export function isValidSlug(slug: string): boolean {
  return SLUG_PATTERN.test(slug)
}

export function isValidTeamKey(key: string): boolean {
  return TEAM_KEY_PATTERN.test(key)
}

/**
 * A workspace name turned into a slug proposal.
 *
 * A *proposal*: the slug field stays editable, and once it has been edited by
 * hand it stops tracking the name (see `WorkspaceStep`). Silently rewriting
 * what someone typed into the field they are looking at is the behaviour that
 * makes a slug field feel broken.
 *
 * `NFKD` before stripping, so "Café" becomes `cafe` rather than `caf`: the
 * decomposition splits the accented letter into a plain `e` plus a combining
 * mark, and the mark is then one of the characters dropped. Without it the
 * whole codepoint is dropped and a name in almost any European language loses
 * letters.
 *
 * Not exhaustive, and does not pretend to be -- a name in a non-Latin script
 * decomposes to nothing here and yields `''`, which is why the caller must
 * treat an empty result as "no proposal" and leave the person to type their
 * own. Transliterating Cyrillic or Han is a library, and this is a
 * convenience.
 */
export function slugify(name: string): string {
  return name
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    // Leading and trailing hyphens are exactly what the server rejects, and
    // they are what any name with punctuation at either end produces.
    .replace(/^-+|-+$/g, '')
}

/**
 * A team name turned into a key proposal -- "Engineering" to `ENG`.
 *
 * Initials when the name has several words ("Design Systems" to `DS`), and
 * the first few letters when it has one. Both are capped at
 * `TEAM_KEY_MAX_LENGTH` and stripped of a leading digit, because a key must
 * start with a letter.
 */
export function proposeTeamKey(name: string): string {
  const words = name
    .normalize('NFKD')
    .toUpperCase()
    .split(/[^A-Z0-9]+/)
    .filter((word) => word.length > 0)

  // `slice` and `join` rather than an index anywhere below.
  // `noUncheckedIndexedAccess` is on and is right to be: `words[0]` is
  // non-undefined only because of a length check the compiler cannot see, and
  // the sliced forms need no check at all.
  const proposal =
    words.length > 1
      ? words.map((word) => word.slice(0, 1)).join('')
      : words.join('').slice(0, 3)

  // A key may not begin with a digit, and "3M" or "24/7 Support" would
  // otherwise propose one. Dropping the leading digits is the smallest thing
  // that keeps the proposal submittable.
  return proposal.replace(/^[0-9]+/, '').slice(0, TEAM_KEY_MAX_LENGTH)
}
