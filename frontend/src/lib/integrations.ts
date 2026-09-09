/**
 * Where a provider's OAuth flow should put the browser back.
 *
 * ## The defect this exists to fix
 *
 * The Connect links used to be built as
 * `` `${startPath}?workspace=${slug}` `` and nothing more. That omission was
 * invisible until a deployment configured GitHub for real, and then it sent
 * every successful connection to the site root.
 *
 * The backend does not guess. `allowed_redirect` in `app/services/github.py`
 * takes the candidate and the deployment's allowlist, and when the candidate
 * is absent it returns `allowlist[0]` -- the FIRST ALLOWED ORIGIN. That is the
 * right answer to "the target was missing or rejected, where does this person
 * belong?", because it is the only value the deployment has stated. But the
 * allowlist holds ORIGINS, so `allowlist[0]` is `https://host` with no path:
 * the main page. A user who pressed Connect in workspace settings finished the
 * whole GitHub flow, was recorded correctly, and landed on the home screen
 * looking like nothing had happened.
 *
 * So the caller says where it wants to come back to, and each call site says
 * it for itself -- settings returns to settings, onboarding returns to its own
 * step. Nothing infers one from the other.
 *
 * ## Why the value is absolute
 *
 * `allowed_redirect` compares the ORIGIN of the candidate against the
 * allowlist, via `urlsplit`. A bare path has no scheme and no netloc, so it
 * parses to no origin, is dropped as "not a URL", and falls straight back to
 * the `allowlist[0]` case this function exists to avoid.
 *
 * Resolving against `window.location.origin` is what makes it absolute, and it
 * is also what keeps this honest: the browser's own origin is not something a
 * caller can talk this page into changing, and whatever it produces still has
 * to survive the server's allowlist check. Nothing here relaxes that check --
 * a URL this function builds for an origin the deployment has not allowed is
 * refused exactly as any other would be.
 */
export function integrationStartHref(
  startPath: string,
  workspaceSlug: string,
  returnToPath: string,
): string {
  const parameters = new URLSearchParams({
    workspace: workspaceSlug,
    return_to: new URL(returnToPath, window.location.origin).toString(),
  })

  return `${startPath}?${parameters.toString()}`
}
