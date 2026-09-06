/**
 * The names this feature knows the auth contract by.
 *
 * Every type is an alias of something generated from `./operations.graphql`.
 * Nothing here declares a field or a nullability, so none of it can disagree
 * with the schema -- change the document, regenerate, and these follow.
 *
 * They exist because the generator names a type after the construct that
 * produced it (`ViewerFieldsFragment`, `LoginMutationVariables`), which says
 * how a type was made rather than what it is, and because two of them are
 * *selections* the generator gives no name of its own.
 */

import type {
  LoginMutation,
  MyWorkspacesQuery,
  ViewerFieldsFragment,
} from '../../../generated/operations'

/**
 * The signed-in user, as every screen in the product sees them.
 *
 * Deliberately not `User` from the schema: this is what the `ViewerFields`
 * fragment selects, which is a strict subset. Naming it after the role rather
 * than the type is also what keeps "the person using the app" distinct from
 * "some user record" once there are member lists to render.
 */
export type Viewer = ViewerFieldsFragment

/**
 * One entry of an auth payload's `errors`.
 *
 * Not a GraphQL error. These arrive inside `data` over a 200 because they
 * describe user input, and the `field`/`code` pairs are a stated contract of
 * `app/services/auth.py` and `app/graphql/mutations/auth.py`:
 *
 *     email       REQUIRED             empty
 *     email       TOO_LONG             > 320 characters
 *     email       INVALID              not an address
 *     email       EMAIL_TAKEN          already registered
 *     password    TOO_SHORT            < 8 characters
 *     password    TOO_LONG             > 1024 characters
 *     name        TOO_LONG             > 200 characters
 *     credentials INVALID_CREDENTIALS  log-in failed, cause not disclosed
 *
 * Register collects every violation before raising, so the list can hold more
 * than one entry and a form must render all of them. `credentials` is the odd
 * one: it names no input, because saying which half was wrong would let a
 * caller enumerate which addresses have accounts.
 */
export type AuthValidationError = LoginMutation['login']['errors'][number]

/** One workspace the viewer belongs to, as `MyWorkspaces` selects it. */
export type ViewerMembership = MyWorkspacesQuery['myWorkspaces'][number]
