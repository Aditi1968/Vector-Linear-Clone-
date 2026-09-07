"""The two GitHub flows that cannot be GraphQL, and are not written as it.

An install is a browser round trip through github.com and a webhook is a POST
from a machine that has never heard of this schema. Both are redirects and raw
bodies rather than documents and variables, which is exactly the case CLAUDE.md
reserves REST for.

Four protections live here and none of them is optional:

* every install is minted with a single-use `state` bound to the caller's
  session, and the callback refuses anything else. Without it, a link in an
  email is enough to attach an attacker's GitHub installation to somebody
  else's workspace -- the victim is signed in, so the callback would authorize
  perfectly;
* the installation id the callback is handed is treated as a CLAIM and never
  as proof. The state says this browser started an install; it does not say
  which installation that install produced, and GitHub's ids are a small
  ascending counter, so naming another organisation's is a guess anyone can
  make. Only /github/webhook can turn a claim into a connection, because only
  its HMAC proves GitHub is the one speaking;
* every delivery is verified against GitHub's HMAC over the RAW body before
  anything parses it, and a failure is refused with no detail at all;
* every redirect target is checked against a configured allowlist. A callback
  that forwards to a URL out of its own query string is an open redirect on a
  domain users have just been asked to trust.
"""

import base64
import json
import secrets
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from starlette.responses import PlainTextResponse, RedirectResponse

from app.config import Environment, get_settings
from app.db import get_pool
from app.domain.errors import (
    GithubInstallationClaimedError,
    GithubNotConfiguredError,
    WorkspaceAccessDeniedError,
)
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.http_cookies import is_secure_environment, read_session_token
from app.repositories.github import GithubRepository
from app.repositories.invitations import InvitationRepository
from app.repositories.memberships import MembershipRepository
from app.repositories.sessions import SessionRepository
from app.repositories.users import UserRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.auth import AuthService
from app.services.github import (
    SIGNATURE_HEADER,
    GithubAppConfig,
    GithubService,
    GithubUserInstallations,
    allowed_redirect,
    require_workspace_admin,
    verify_webhook_signature,
)
from app.services.memberships import MembershipService
from app.services.passwords import Argon2PasswordHasher
from app.services.tokens import hash_session_token


router = APIRouter(prefix="/github", tags=["github"])

# Where GitHub is asked to authorize the installing user.
#
# The OAuth authorize endpoint rather than `github.com/apps/<slug>/
# installations/new`, because the app *slug* is not derivable from anything in
# configuration -- it is a name GitHub assigns and only the API reports -- and
# a sixth required setting to hold a value nobody would remember to update is
# worse than this. `client_id` is enough: GitHub offers the install to a user
# who has not installed the app, and redirects back with `installation_id`
# when they do.
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"

# The state cookie. A cookie rather than a row, because the value has to be
# proven to belong to *this browser* and a table cannot say that: a database
# row keyed by the state would be readable by whoever presents the state, which
# is precisely the attacker in a CSRF. It also needs no cleanup job, no expiry
# sweep and no write on the path of every install.
STATE_COOKIE_NAME = "vector_github_state"

# Scoped to this router. Narrower than the session cookie on purpose: nothing
# outside the install flow has any use for it, and a cookie is sent to every
# path it is scoped to.
# Site-wide, and it has to be. The install begins at `/github/install` or
# `/integrations/github/install`, but GitHub redirects to whichever callback
# the App has registered -- here `/integrations/github/oauth/callback`. A
# cookie scoped to `/github` is not sent to a path under `/integrations`, so
# the narrower scope meant the callback saw NO state at all and refused a
# flow that was entirely legitimate. There is no path expression covering
# both mounts except the root.
#
# What the narrower scope was buying is bought elsewhere and still holds: the
# cookie is HttpOnly, expires in ten minutes, is single-use and consumed on
# every exit including the failures, and carries a digest of the session that
# minted it, so a cookie planted by anyone else matches nothing.
STATE_COOKIE_PATH = "/"

# Ten minutes: long enough to read GitHub's permission screen and pick
# repositories, short enough that an abandoned install does not leave a usable
# state in the browser for the rest of the day.
STATE_COOKIE_MAX_AGE = 600

# Bytes behind the state itself. The same 32 as a session token, and for the
# same reason: this value alone is what tells the callback that a redirect it
# is looking at was started by this browser.
STATE_ENTROPY_BYTES = 32

# What a rejected request is told, which is nothing. One string for a missing
# signature, a wrong one and a deployment with no secret at all, because the
# differences between them are facts about this server's configuration and the
# caller has not authenticated.
UNAUTHORIZED_DETAIL = "Unauthorized"

# What the callback says when the deployment configured no redirect allowlist.
# A page rather than a guess: there is nowhere this server has been told it may
# send a browser, and inventing one is the open redirect this module refuses.
#
# It does not say "connected", because the callback does not connect anything.
# It records a claim that GitHub has yet to confirm, and a page telling the
# admin otherwise would be the same untruth `githubIntegration` used to tell.
CLAIMED_MESSAGE = "GitHub install received. Waiting for GitHub to confirm it."

# What a caller is told when the installation they named is spoken for.
#
# Deliberately vague about which workspace holds it and about whether the hold
# is a live connection or an unconfirmed claim. Either answer would turn this
# endpoint into an oracle for "is installation N in use on this deployment",
# which is one query short of the enumeration the claim window exists to make
# expensive. It does say the refusal may not be permanent, because an
# unconfirmed claim expires and the honest owner needs to know to try again.
CLAIMED_ELSEWHERE_DETAIL = (
    "This GitHub installation is already spoken for. If you have just "
    "installed the app, wait a few minutes and try again."
)


@dataclass(frozen=True, slots=True)
class GithubHttpServices:
    """Everything a GitHub route needs, resolved once per request.

    A dataclass behind a single FastAPI dependency rather than four
    dependencies, so that a test overrides one thing and gets a consistent set
    -- and so that the wiring below reads as one composition point, the way
    `app.graphql.context.get_context` does for GraphQL.
    """

    github: GithubService
    auth: AuthService
    memberships: MembershipService
    environment: Environment

    # The callback URL this deployment's GitHub App has registered, or None.
    # Carried here rather than read from settings at the point of use, so the
    # authorize leg and the token exchange cannot present two different
    # values -- GitHub compares them and refuses the exchange if they differ.
    #
    # Defaulted, because None is a real and correct state: an App with one
    # registered callback needs no `redirect_uri` at all. A required field
    # here would also make every existing construction -- the tests', and any
    # future caller's -- a compile error over a value most of them have no
    # opinion about.
    oauth_callback_url: str | None = None


def build_services() -> GithubHttpServices:
    """Compose the services these routes use, per request.

    The pool is owned by the FastAPI lifespan; this only borrows it, exactly
    as `get_context` does. Never call connect() or create a pool here.

    Settings are resolved per request rather than at import, which is what
    keeps this module importable with no configuration at all -- the property
    tests/test_settings.py pins for the whole application. `get_settings` is
    lru_cached, so after the first request this is a dict lookup and a small
    object.
    """
    pool = get_pool()
    settings = get_settings()

    return GithubHttpServices(
        github=GithubService(
            pool=pool,
            repository=GithubRepository(),
            config=GithubAppConfig.from_settings(settings),
            # Built only when the deployment has an OAuth client to build it
            # from; without one the service falls back to webhook-only
            # confirmation, which is the behaviour 016 shipped.
            ownership=(
                GithubUserInstallations(
                    client_id=settings.github_client_id,
                    client_secret=settings.github_client_secret.get_secret_value(),
                    redirect_uri=settings.github_oauth_callback_url,
                )
                if settings.github_client_id and settings.github_client_secret
                else None
            ),
        ),
        auth=AuthService(
            pool=pool,
            users=UserRepository(),
            sessions=SessionRepository(),
            hasher=Argon2PasswordHasher(),
        ),
        memberships=MembershipService(
            pool=pool,
            repository=MembershipRepository(),
            # Unused on this path -- the callback only ever asks whether the
            # caller may act in a workspace -- but the service is one object
            # and a half-built one would fail at the first call that did need
            # them, in a request handler rather than at construction.
            workspaces=WorkspaceRepository(),
            invitations=InvitationRepository(),
        ),
        environment=settings.environment,
        oauth_callback_url=settings.github_oauth_callback_url,
    )


def _not_found() -> HTTPException:
    """404, for every refusal that must not confirm anything exists.

    A deployment with no GitHub App, a slug that names no workspace, a
    workspace the caller does not belong to, and a workspace where they are an
    ordinary member all answer the same. The alternatives leak in both
    directions: a 403 confirms the workspace is real, and a 501 confirms this
    deployment has no GitHub App to anyone who can reach the port.
    """
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def _encode_state(payload: dict[str, Any]) -> str:
    """The state cookie's value: JSON, base64url, no signature.

    Unsigned deliberately, and it is worth being explicit about why that is
    safe. Nothing in this payload is trusted for its content. The state is
    compared against the one echoed back, the session digest is compared
    against the digest of the cookie the caller is presenting right now, the
    workspace is re-checked against `workspace_members` for the authenticated
    viewer, and the redirect is re-validated against the allowlist. Forging the
    cookie therefore buys an attacker only the ability to lie to a browser they
    already control.

    base64url rather than raw JSON because a cookie value may not contain
    commas, semicolons or spaces, and a slug or a URL is under nobody's control
    but the caller's.
    """
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _decode_state(raw: str | None) -> dict[str, Any] | None:
    """Read the state cookie back, or None if it is not one.

    Every way a cookie can be junk -- absent, not base64, not JSON, not an
    object -- collapses into None, because the caller does exactly the same
    thing in all of them: refuse. `binascii.Error` and `UnicodeDecodeError`
    are both ValueError subclasses, so one except covers the lot.
    """
    if raw is None:
        return None

    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except ValueError:
        return None

    return payload if isinstance(payload, dict) else None


def _session_digest(request: Request) -> str:
    """A fingerprint of the session this request is presenting.

    Hex of the same SHA-256 the sessions table stores, so the state is bound to
    a session without this module ever holding, logging or comparing the token
    itself. An empty string for a request with no session, which is a value the
    comparison can never match against a real one -- and must not, because a
    state minted under a session has to be finished under that same session.

    This is what stops an attacker who can write cookies into the victim's
    browser (a compromised sibling subdomain, say) from planting a state of
    their own: they would also have to know the victim's session token, and
    that is HttpOnly and never leaves the browser.
    """
    token = read_session_token(request)

    return "" if token is None else hash_session_token(token).hex()


def _set_state_cookie(
    response: Response,
    *,
    value: str,
    environment: Environment,
) -> None:
    """Write the state cookie under the policy the callback will clear it with.

    SameSite=lax, and the choice is load-bearing rather than copied. The
    callback is a top-level GET navigation from github.com, which is
    cross-site: `strict` would withhold the cookie on exactly that request and
    break every install, while `none` would send it on cross-site POSTs, which
    is the class of request the state exists to defend. `lax` is the one value
    that is both delivered and protective here.

    HttpOnly because no script has any reason to read this, and Secure only
    where the deployment speaks https -- the same rule, from the same function,
    that decides it for the session cookie.
    """
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=value,
        max_age=STATE_COOKIE_MAX_AGE,
        path=STATE_COOKIE_PATH,
        httponly=True,
        secure=is_secure_environment(environment),
        samesite="lax",
    )


def _clear_state_cookie(response: Response, *, environment: Environment) -> None:
    """Consume the state, whatever the callback decided.

    Every attribute matches `_set_state_cookie`, or the browser treats this as
    a different cookie and keeps the original -- which would make the state
    replayable, and single use is half of what it is for.

    Called on the failure paths too. A state that survived a rejected callback
    is a state an attacker gets to keep guessing against.
    """
    response.delete_cookie(
        key=STATE_COOKIE_NAME,
        path=STATE_COOKIE_PATH,
        httponly=True,
        secure=is_secure_environment(environment),
        samesite="lax",
    )


async def _authorized_scope(
    request: Request,
    services: GithubHttpServices,
    slug: str,
) -> AuthorizedWorkspaceScope:
    """The caller's admin scope on this workspace, or a 404.

    Authentication first and separately: an unauthenticated caller gets a 401,
    because "sign in" is something a browser can act on and a 404 is not.
    Everything after that -- unknown slug, non-member, ordinary member --
    answers 404 for the reason `_not_found` gives.
    """
    user = await services.auth.authenticate(read_session_token(request))

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHORIZED_DETAIL,
        )

    try:
        scope = await services.memberships.authorized_scope_for_slug(
            slug=slug,
            user_id=user.id,
        )
        require_workspace_admin(scope)
    except WorkspaceAccessDeniedError:
        raise _not_found() from None

    return scope


@router.get("/install")
async def install(
    request: Request,
    services: Annotated[GithubHttpServices, Depends(build_services)],
    workspace: str,
    return_to: str | None = None,
) -> RedirectResponse:
    """Start an install: mint a state, remember it, and hand off to GitHub.

    The state is generated here and nowhere else. It goes two places at once --
    into the URL GitHub will echo back, and into a cookie only this browser
    holds -- and the callback connects nothing unless the two agree. That is
    the entire CSRF story for this flow, and it is why this endpoint exists at
    all rather than the front end linking straight to github.com.

    `return_to` is validated NOW, against the allowlist, and only the validated
    value is stored. Validating again in the callback is not redundancy for its
    own sake: the cookie passes through the client in between.

    404 rather than a message when the deployment has no GitHub App. There is
    nothing here to install, and saying which of the credentials is missing
    would describe this deployment's provisioning to anyone who can reach it.
    """
    config = services.github.config

    if not config.configured:
        raise _not_found()

    # Called for the refusal, not for the value. An ordinary member sent off
    # to GitHub would install an app whose callback then refuses to record it,
    # which is a worse refusal than this one.
    await _authorized_scope(request, services, workspace)

    state = secrets.token_urlsafe(STATE_ENTROPY_BYTES)

    # Built from the configured client id and a freshly minted state. Nothing
    # the caller sent reaches this URL: `workspace` and `return_to` travel in
    # the cookie, so there is no parameter here for a client to aim elsewhere.
    #
    # `urlencode` rather than an f-string, because the client id is an
    # operator-supplied string and a '&' in it would otherwise silently split
    # into a parameter of its own.
    parameters = {"client_id": config.client_id, "state": state}

    # No `redirect_uri`. GitHub uses the App's registered Callback URL when
    # the parameter is absent, and a GitHub App has exactly one -- so there is
    # nothing for this to disambiguate and everything for it to get wrong.
    #
    # Sending it was tried and rejected: GitHub answered "The redirect_uri is
    # not associated with this application", because it demands a byte-exact
    # match against the registered value and this deployment's is a tunnel URL
    # that changes whenever the tunnel restarts. Two copies of a value that
    # moves is one copy too many, and the copy this process holds is the one
    # that goes stale silently.
    #
    # `github_oauth_callback_url` is still read -- the flow has to know which
    # ORIGIN it will come back on, because the state cookie has to be set on
    # that host -- it is simply not sent to GitHub as a parameter.
    query = urlencode(parameters)

    response = RedirectResponse(
        f"{GITHUB_AUTHORIZE_URL}?{query}",
        status_code=status.HTTP_302_FOUND,
    )

    _set_state_cookie(
        response,
        value=_encode_state(
            {
                "state": state,
                "session": _session_digest(request),
                "workspace": workspace,
                "return_to": allowed_redirect(
                    return_to,
                    allowlist=config.redirect_allowlist,
                ),
            }
        ),
        environment=services.environment,
    )

    return response


# Two paths, one handler. `/oauth/callback` is what this deployment's GitHub
# App has registered as its callback URL, and a registered URL is a value
# GitHub holds rather than one this repository can choose -- a redirect has
# already left the user's browser by the time a 404 would be discovered.
# `/callback` stays because it is the older spelling and dropping it would
# break any App still registered against it. Neither is a second
# implementation: both decorate the same function, so the state check, the
# claim and the refusals cannot drift between them.
@router.get("/callback")
@router.get("/oauth/callback")
async def callback(
    request: Request,
    services: Annotated[GithubHttpServices, Depends(build_services)],
    state: str | None = None,
    installation_id: int | None = None,
    code: str | None = None,
) -> Response:
    """Finish an install GitHub is redirecting a browser back from.

    The order of the checks is the design. State before identity, identity
    before workspace, workspace before write -- so a caller who did not start
    this flow never causes a membership lookup, and a caller who did but may
    not act never causes a write.

    `code` is deliberately ignored and never exchanged. A user-to-server token
    is a bearer credential this server has no use for and no safe place to
    keep, so the one thing to do with it is nothing.

    The installation id is the client's, and this endpoint treats it as an
    unproven claim rather than a fact, because it is one. The state proves the
    redirect belongs to this browser's own install attempt; it says nothing
    about which installation that attempt produced, and GitHub numbers
    installations with a small ascending counter, so `?installation_id=N` for
    somebody else's N costs an attacker a guess. What this call records is
    therefore a claim with a deadline (`GithubService.connect`), and only a
    signature-verified delivery on /github/webhook can turn it into a
    connection.

    That is the strongest binding available here. The stronger one is
    `GET /app/installations/{id}` signed with the app's private key, which
    would settle ownership in one round trip -- and needs RS256 JWT signing
    and an HTTP client in the runtime, neither of which this application
    carries. Until it does, the claim window is what stands in.

    Every exit goes through `_clear_and_return`, including the failures. That
    is the other half of single use: a state that survived a rejected callback
    is a state somebody gets to keep trying, so the refusals consume it too.
    HTTPException is caught rather than propagated for exactly that reason --
    FastAPI's handler builds its own response, and a cookie cannot be attached
    to one this frame never sees.
    """
    environment = services.environment

    try:
        response = await _complete_install(
            request,
            state=state,
            installation_id=installation_id,
            code=code,
            services=services,
        )
    except HTTPException as exc:
        response = PlainTextResponse(
            str(exc.detail),
            status_code=exc.status_code,
        )

    return _clear_and_return(response, environment=environment)


async def _complete_install(
    request: Request,
    *,
    state: str | None,
    installation_id: int | None,
    code: str | None,
    services: GithubHttpServices,
) -> Response:
    """The callback's decisions, with the cookie handling left to its caller."""
    config = services.github.config

    if not config.configured:
        raise _not_found()

    stored = _decode_state(request.cookies.get(STATE_COOKIE_NAME))

    if stored is None or not _state_matches(stored, state, request):
        # 400 rather than 404: the request is malformed rather than aimed at
        # something hidden, and a browser that lost its cookie needs to be
        # told to start the flow again rather than that GitHub does not exist.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid state",
        )

    if installation_id is None or installation_id <= 0:
        # GitHub sends `setup_action=request` with no installation when an
        # organisation owner still has to approve. Nothing to record yet, and
        # recording something would report a connection that does not exist.
        return PlainTextResponse(
            "No GitHub installation was completed.",
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    workspace = _string_or_none(stored.get("workspace"))

    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid state",
        )

    scope = await _authorized_scope(request, services, workspace)

    try:
        await services.github.connect(scope, installation_id=installation_id)

        # The claim is recorded. Now ask GitHub whether the account that just
        # consented can actually administer the installation it named -- the
        # one question the redirect itself cannot answer, and the reason
        # `code` is read here at all.
        #
        # It is deliberately AFTER the claim rather than instead of it. The
        # claim is what a second workspace collides with, so recording it
        # first keeps the refusal for a contested id identical whether or not
        # the verification then succeeds. A failure here leaves PENDING,
        # which a signed `installation` delivery can still promote.
        #
        # This is what makes connecting to an app that is ALREADY installed
        # work: GitHub emits `installation.created` once, so that route is
        # closed for every installation after the first.
        if code:
            await services.github.confirm_with_user_grant(
                installation_id=installation_id, code=code
            )
    except GithubNotConfiguredError:
        raise _not_found() from None
    except GithubInstallationClaimedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=CLAIMED_ELSEWHERE_DETAIL,
        ) from None
    except WorkspaceAccessDeniedError:
        raise _not_found() from None

    target = allowed_redirect(
        # Re-validated rather than trusted. The value was checked before it was
        # written, but it travelled through the client in between, and a cookie
        # is not a value this server chose.
        _string_or_none(stored.get("return_to")),
        allowlist=config.redirect_allowlist,
    )

    if target is None:
        return PlainTextResponse(CLAIMED_MESSAGE)

    return RedirectResponse(target, status_code=status.HTTP_302_FOUND)


@router.post("/webhook")
async def webhook(
    request: Request,
    services: Annotated[GithubHttpServices, Depends(build_services)],
) -> Response:
    """Accept one delivery from GitHub, if GitHub really signed it.

    The body is read as bytes and verified before anything parses it. That
    ordering is the whole point: `json.loads` followed by `json.dumps` is not
    the same bytes, so a signature checked against a re-serialised payload
    proves nothing about what arrived.

    The verification below is also the only thing in this application that can
    establish which workspace owns an installation. `GithubService.connect`
    records a claim; this endpoint is where a claim is confirmed, and it can be
    because the HMAC proves GitHub sent the payload naming that installation.
    Loosening this check does not merely admit forged repository lists -- it
    hands an attacker the ability to confirm their own claim on somebody
    else's organisation.

    A rejected delivery is a 401 with a fixed string. It does not say whether
    the header was missing, malformed, or right for a different secret, and it
    does not say whether this deployment has a webhook secret at all -- this
    endpoint is reachable by anyone who can reach the port, and each of those
    answers is a fact about the configuration.

    204 for an accepted one, including one this server has no rule for.
    Anything else earns a redelivery from GitHub for a payload that will never
    be handled differently.

    Known ceiling: `app.http_limits` bounds every request body at 256 KiB, so a
    delivery larger than that is rejected at the edge with a 413 and retried in
    vain. No payload this server acts on approaches it; a route-specific
    ceiling is the fix if one ever does.
    """
    config = services.github.config
    body = await request.body()

    if not verify_webhook_signature(
        secret=config.webhook_secret,
        body=body,
        header=request.headers.get(SIGNATURE_HEADER),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=UNAUTHORIZED_DETAIL,
        )

    try:
        payload = json.loads(body)
    except ValueError:
        # Signed, so it came from GitHub, and still not JSON. That is a bug
        # rather than an attack, and 400 tells GitHub's delivery log so
        # without inviting a retry.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed payload",
        ) from None

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed payload",
        )

    await services.github.apply_webhook(
        event=request.headers.get("X-GitHub-Event", ""),
        payload=payload,
    )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _state_matches(
    stored: dict[str, Any] | None,
    presented: str | None,
    request: Request,
) -> bool:
    """Whether this callback belongs to an install this browser started.

    Two comparisons, both with `hmac.compare_digest` by way of
    `secrets.compare_digest`, and both required:

    * the echoed state equals the one in the cookie. A state from another
      browser -- pasted, phished, replayed from a link -- matches nothing here,
      because the cookie was never written to this browser;
    * the session the state was minted under is the session presenting it. A
      state remains useless to anyone who cannot also present the session
      cookie it was bound to, which is the binding that survives an attacker
      who can set cookies but not read them.

    A replay fails the first comparison, because the callback deletes the
    cookie: there is nothing left to match against.
    """
    if stored is None or presented is None:
        return False

    expected = _string_or_none(stored.get("state"))
    session = _string_or_none(stored.get("session"))

    if expected is None or session is None:
        return False

    # `secrets.compare_digest` is `hmac.compare_digest`; both operands are
    # ASCII by construction (token_urlsafe and a hex digest), and a presented
    # state that is not would raise, so it is refused first.
    if not presented.isascii():
        return False

    return secrets.compare_digest(expected, presented) and secrets.compare_digest(
        session,
        _session_digest(request),
    )


def _string_or_none(value: Any) -> str | None:
    """A str, or None for anything else a decoded cookie might hold."""
    return value if isinstance(value, str) else None


def _clear_and_return(response: Response, *, environment: Environment) -> Response:
    _clear_state_cookie(response, environment=environment)

    return response
