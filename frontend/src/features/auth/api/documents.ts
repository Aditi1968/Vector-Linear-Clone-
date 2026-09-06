/**
 * The seam between codegen's output and this feature.
 *
 * Nothing outside `./` imports an auth document from `src/generated`, so a
 * change of generated layout -- a different directory, a preset, a naming
 * convention -- lands in this one import rather than in every hook.
 *
 * `MeDocument` is the exception to "no document leaves the api directory":
 * ./sessionExpiry writes through it from inside an Apollo link, and tests
 * mock responses with it. Both need the exact document that produced the
 * result, which no hook can hand them.
 */

export {
  LoginDocument,
  LogoutDocument,
  MeDocument,
  MyWorkspacesDocument,
  RegisterDocument,
} from '../../../generated/operations'
