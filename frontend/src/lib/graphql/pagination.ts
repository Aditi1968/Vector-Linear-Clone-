/**
 * Page sizes, chosen against the backend's real limits rather than by taste.
 *
 * Two independent ceilings apply to an `issues` query, and they fail in
 * different ways, so both are worth knowing before picking a number.
 *
 * 1. The service refuses a `first` outside 1..100 (`app/services/issues.py`,
 *    FIRST_MIN / FIRST_MAX). It does *not* silently clamp: an out-of-range
 *    value comes back as a top-level GraphQL error with
 *    `extensions.code = "BAD_USER_INPUT"`, not as a shorter page.
 *
 * 2. The GraphQL layer refuses documents over a complexity budget of 1000
 *    during *validation*, before any resolver runs (`app/graphql/limits.py`,
 *    MAX_COMPLEXITY). The cost of an `issues` selection is
 *
 *        first x (fields selected per node + fields selected on pageInfo)
 *
 *    so a page asking for all seven `Issue` fields plus both `pageInfo`
 *    fields costs `first x 9`. At `first: 100` that is 900 of the 1000
 *    available -- a single full page nearly exhausts the budget on its own.
 *
 * The practical consequence of (2) is a rule about document *shape*, not
 * just page size: two full `issues(first: 100)` selections in one document
 * cost 1800 and are rejected outright. Keep list queries to one `issues`
 * field per document rather than batching two into a single request. (Two
 * selections at the backend's declared default of 50 come to exactly 900 and
 * do fit, but with 100 to spare -- close enough to the ceiling that adding
 * one more field to either selection breaks the document.)
 */

/** Hard ceiling from `app.services.issues.FIRST_MAX`. Never send more. */
export const MAX_PAGE_SIZE = 100

/**
 * Rows per page for list views.
 *
 * Modest on purpose. At 25 a fully-selected page costs 225 of the 1000
 * complexity budget, which leaves room for a document to grow a field or a
 * sibling selection without a client-side change becoming a server-side
 * rejection. It is also more rows than fit on a screen, so "load more" stays
 * a deliberate action rather than something the first paint triggers.
 */
export const DEFAULT_PAGE_SIZE = 25
