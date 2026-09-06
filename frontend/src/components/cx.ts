/**
 * Join class names, dropping anything absent.
 *
 * Two things make this worth a module rather than a template literal at each
 * call site.
 *
 * First, CSS Modules are typed as `{ readonly [key: string]: string }`, and
 * `noUncheckedIndexedAccess` (see tsconfig.app.json) therefore widens every
 * `styles.foo` to `string | undefined`. A template literal accepts that
 * silently and renders the text `undefined` into `class` when a lookup is
 * wrong -- a typo'd class name becomes an invisible bug instead of a type
 * error. Filtering here turns it into nothing at all.
 *
 * Second, it makes conditional classes expression-shaped:
 * `cx(styles.link, isActive && styles.active)`. `false` and `null` are
 * accepted for exactly that reason.
 *
 * Not a `classnames`/`clsx` dependency: object and array syntax are not used
 * anywhere in this codebase, and this is the whole of what those libraries
 * would be doing for us.
 */
export type ClassValue = string | false | null | undefined

export function cx(...values: readonly ClassValue[]): string {
  return values.filter((value): value is string => Boolean(value)).join(' ')
}
