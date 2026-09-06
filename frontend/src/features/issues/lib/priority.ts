/**
 * How this frontend presents `Issue.priority`.
 *
 * ## What the server provides
 *
 * An integer, and nothing else. `app/services/issues.py` enforces
 * `PRIORITY_MIN = 0` through `PRIORITY_MAX = 4` and `IssueCreateInput`
 * defaults it to 0. There is no priority enum in the schema, no name, no
 * colour, no declared ordering, and no statement anywhere in the backend
 * about whether 0 or 4 is the urgent end.
 *
 * ## What this file adds, and why it is labelled
 *
 * The names below are a **frontend convention**. They are the convention
 * Linear uses -- 0 is "no priority" and is also the create default, and 1
 * through 4 run from most to least urgent -- which is the one a Linear-style
 * product should adopt, and which makes the server's default (0) mean
 * "unset" rather than "the most urgent thing in the tracker".
 *
 * Because it is invented, it is never presented as though the server said it:
 * every rendering of a priority shows the integer alongside the name, and the
 * list and the composer both state in words that the names are a client-side
 * convention. Two integers with two different names and no indication that a
 * human picked them is how a UI convention gets mistaken for an API contract
 * and then depended on by something else.
 *
 * If the backend later grows a real priority enum, this file is where it
 * lands, and the disclaimers around it come out at the same time.
 */

export const PRIORITY_MIN = 0
export const PRIORITY_MAX = 4

/**
 * The names, in ascending numeric order so the index is the priority.
 *
 * `as const` and a tuple rather than an object literal keyed by number: the
 * index *is* the wire value, and a tuple keeps that relationship visible.
 */
const PRIORITY_NAMES = ['No priority', 'Urgent', 'High', 'Medium', 'Low'] as const

/**
 * Every priority the server will accept, for building a selector.
 *
 * Derived from the names rather than written out again, so the two cannot
 * disagree about how many there are.
 */
export const PRIORITY_VALUES: readonly number[] = PRIORITY_NAMES.map(
  (_name, index) => index,
)

export interface PriorityPresentation {
  /** The raw integer, exactly as the server has it. */
  value: number
  /** The frontend's name for it, or null if the server sent something unexpected. */
  name: string | null
  /** What a screen reader should say, disclaimer included. */
  label: string
}

/**
 * Present one priority value.
 *
 * Tolerates a value outside 0..4 by naming it `null` rather than by clamping
 * or by picking the nearest name. The service will not produce one today, but
 * a UI that guesses a name for an unrecognised value is a UI that will keep
 * displaying "Low" long after the range changed underneath it.
 */
export function describePriority(value: number): PriorityPresentation {
  const name = PRIORITY_NAMES[value] ?? null

  return {
    value,
    name,
    label:
      name === null
        ? `Priority ${value}`
        : `Priority ${value}, labelled ${name} by this app`,
  }
}
