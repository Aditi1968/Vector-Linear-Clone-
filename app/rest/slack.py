"""Slack's two protocol endpoints: the OAuth round trip and the event feed.

REST rather than GraphQL, and this is the case CLAUDE.md reserves REST for.
Neither of these is a first-party product call. The OAuth callback is a
browser redirect Slack performs with query parameters of its choosing, and the
events endpoint is a signed POST whose body Slack defines and whose response
Slack interprets -- there is no query document to write and no client of ours
to write it.

Everything in this module is transport: signature checking, state checking,
cookie handling, and turning a verified request into one service call. Every
rule about what a caller may do lives in app/services/slack.py.

Three properties are load-bearing and each is argued where it is implemented:

* the events endpoint verifies Slack's signature over the RAW body before
  anything parses it (`verify_slack_signature`),
* the OAuth callback validates a state it issued, bound to the browser that
  started the flow and consumed on use (`_parse_state_cookie` and the callback
  itself),
* nothing is ever redirected to a URL taken from a request
  (`resolve_return_path`).
"""

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import Environment, get_settings
from app.db import get_pool
from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.slack import SlackOAuthError, SlackTeamAlreadyConnectedError
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.http_cookies import is_secure_environment, read_session_token
from app.repositories.invitations import InvitationRepository
from app.repositories.memberships import MembershipRepository
from app.repositories.rate_limits import RateLimitRepository
from app.repositories.sessions import SessionRepository
from app.repositories.slack import SlackRepository
from app.repositories.users import UserRepository
from app.repositories.workspaces import WorkspaceRepository
from app.rest.oauth_origin import redirect_to_callback_origin
from app.services.auth import AuthService
from app.services.memberships import MembershipService
from app.services.passwords import Argon2PasswordHasher
from app.services.slack import (
    DatabaseTokenStore,
    SlackOAuthClient,
    SlackOAuthExchange,
    SlackService,
    authorize_url,
)
from app.services.tokens import hash_session_token


router = APIRouter(prefix="/slack", tags=["slack"])


# --- Slack's request signing scheme -----------------------------------

# The version prefix Slack signs with. A constant rather than a literal
# because it appears twice -- in the string that is signed and in the header
# that is compared -- and the two must be the same version or every signature
# fails in a way that looks like a wrong secret.
SIGNATURE_VERSION = "v0"

# How stale a signed request may be. Five minutes is Slack's own window and it
# is part of the scheme rather than a hardening extra: the signature covers the
# timestamp, so an attacker who captures one valid request can replay it
# verbatim forever, and the timestamp check is the only thing that expires it.
#
# Applied in both directions. A request from the future is not more trustworthy
# than one from the past -- a clock that has run ahead produces one, and so
# does an attacker holding a captured request and a machine whose clock they
# set -- so the comparison is on absolute distance.
MAX_TIMESTAMP_AGE_SECONDS = 300

TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
SIGNATURE_HEADER = "X-Slack-Signature"


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: bytes,
    now: float,
) -> bool:
    """Whether this is a request Slack signed, recently, with our secret.

    The v0 scheme: HMAC-SHA256 over the bytes `v0:{timestamp}:{body}`, keyed
    with the signing secret, rendered as lowercase hex and prefixed `v0=`.

    Three details are the whole security of this function.

    The body is RAW bytes and is never parsed first. A signature is over
    exactly the bytes that were sent, so verifying a re-serialised body would
    verify something Slack did not sign -- and every JSON round trip is a
    chance to reorder keys, rewrite numbers or normalise unicode. It also
    means a malformed body is rejected as unsigned rather than being handed to
    a parser by an unauthenticated caller.

    The comparison is `hmac.compare_digest`. `==` on strings returns as soon
    as two characters differ, so the time it takes reveals how long a prefix
    was correct, and a signature can be recovered one character at a time by
    an attacker who can measure it. compare_digest takes the same time for
    every input of a given length.

    The timestamp is checked BEFORE the signature is trusted and independently
    of it. A correctly signed request from an hour ago is a replay, and the
    signature says nothing about that -- it is valid, which is the point.

    `now` is a parameter rather than a call to `time.time()` inside, so a test
    can present a request that is genuinely stale, or genuinely fresh, without
    depending on when it runs.

    Returns a bool rather than raising, and the caller answers every false
    identically. There is deliberately no way for a caller to learn WHICH
    check failed: distinguishing "bad signature" from "stale" from "no header"
    tells an attacker whether the secret is right, which is the one bit this
    endpoint must not leak.
    """
    if not timestamp or not signature:
        return False

    try:
        signed_at = int(timestamp)
    except ValueError:
        return False

    try:
        outside_window = abs(now - signed_at) > MAX_TIMESTAMP_AGE_SECONDS
    except OverflowError:
        # `now` is a float, so the comparison converts `signed_at` to one, and
        # a Python int is unbounded where a float is not: a header of a few
        # hundred digits raises here rather than comparing. Uncaught, that is
        # an unauthenticated 500 on a public endpoint -- a header anyone can
        # send, before any signature is checked. A timestamp too large to be a
        # float is not within five minutes of now.
        return False

    if outside_window:
        return False

    # Assembled as bytes rather than by formatting a string and encoding it.
    # The body is arbitrary bytes -- Slack sends UTF-8, but a decode step here
    # would be one more place a request could fail on its content rather than
    # on its signature.
    signed = b"%s:%s:%s" % (
        SIGNATURE_VERSION.encode("ascii"),
        timestamp.encode("utf-8"),
        body,
    )

    expected = "{}={}".format(
        SIGNATURE_VERSION,
        hmac.new(
            signing_secret.encode("utf-8"),
            signed,
            hashlib.sha256,
        ).hexdigest(),
    )

    # Both sides encoded: compare_digest refuses a str containing anything
    # outside ASCII, and the header is attacker-controlled, so comparing
    # strings would turn a crafted header into a TypeError -- a 500 where a
    # 401 belongs.
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


# --- the OAuth state cookie -------------------------------------------

# Bytes of entropy behind one state value, matching
# app.services.tokens.TOKEN_ENTROPY_BYTES. The state's only job is to be
# unguessable by whoever might forge a callback, and 256 bits from `secrets`
# is not a thing anyone guesses.
STATE_ENTROPY_BYTES = 32

# Named separately from the session cookie because it is a different cookie
# with a different life. It exists for the seconds an admin spends on Slack's
# consent screen and is deleted the moment the callback consumes it.
OAUTH_STATE_COOKIE_NAME = "vector_slack_oauth"

# Scoped to the OAuth routes, unlike the session cookie's "/". Nothing outside
# this flow has any use for it, so nothing outside this flow is sent it -- one
# fewer credential-shaped value riding on every request to the API.
# Site-wide, for the reason app/rest/github.py gives at STATE_COOKIE_PATH:
# the flow starts under `/slack/oauth` or `/integrations/slack/oauth` and
# Slack redirects to the registered callback under `/integrations`, which a
# cookie scoped to `/slack/oauth` is never sent to. The protections the
# narrower scope was standing in for -- HttpOnly, a short expiry, single use,
# and the session digest checked in the callback -- are all still here.
OAUTH_STATE_COOKIE_PATH = "/"

# Ten minutes: long enough to read a consent screen and sign in to Slack,
# short enough that an abandoned flow does not leave a valid state in a
# browser for a day.
OAUTH_STATE_TTL_SECONDS = 600

# Lax, and this is the one cookie in the codebase where Strict would be a bug
# rather than a preference. The callback is a cross-site top-level navigation
# -- Slack's servers redirect the browser back here -- and Strict withholds
# the cookie on exactly that, so the flow would fail every time with a state
# that could never match. Lax sends it on a top-level GET, which is what the
# callback is, and still withholds it from cross-site POSTs.
OAUTH_STATE_COOKIE_SAMESITE: Literal["lax"] = "lax"

# Where the flow may send a browser when it is done.
#
# An allowlist of exact paths, not a validator. Every "is this URL safe"
# function is a parser competing with a browser's, and browsers disagree with
# parsers about `//evil.com`, `/\evil.com`, `https:/evil.com`, backslashes,
# unicode and userinfo. Nothing outside this set is ever emitted, so none of
# that has to be got right.
#
# Add the workspace settings path here when the frontend grows one.
# Where a finished flow may land, as a template rather than a fixed path.
#
# `/` was the default and it is the PUBLIC landing page: a successful connect
# dropped the admin on a marketing page with a Sign In button, which reads as
# having been signed out. `/issues` is worse -- it predates workspace-scoped
# routing and now resolves to a workspace whose slug is "issues".
#
# The workspace is known here (the state cookie recorded it), so the flow
# returns to the screen it started from. `{slug}` is substituted from that
# recorded value and never from anything in the request.
RETURN_PATH_TEMPLATES = frozenset({"/{slug}/settings", "/{slug}/issues"})
DEFAULT_RETURN_TEMPLATE = "/{slug}/settings"


def resolve_return_path(requested: str | None) -> str:
    """The template to send a browser to after the flow, from the allowlist.

    A request naming anything else is not an error: it lands on the default.
    Erroring would make an unrecognised path a dead end at the very end of a
    successful OAuth grant, which is the worst moment to fail -- the
    installation has already happened.

    Returns a TEMPLATE, not a path. The workspace is filled in by
    `_redirect_to` from the value the state cookie recorded, so nothing a
    request supplies can steer where the browser ends up.
    """
    if requested is not None and requested in RETURN_PATH_TEMPLATES:
        return requested

    return DEFAULT_RETURN_TEMPLATE


def _session_digest(request: Request) -> str:
    """A fingerprint of the session this request is presenting.

    Hex of the same SHA-256 the sessions table stores, so a state is bound to
    a session without this module ever holding, logging or comparing the token
    itself. An empty string for a request with no session, which is a value the
    comparison can never match against a real one -- and must not, because a
    state minted under a session has to be finished under that same session.

    The same helper app/rest/github.py has, for the same reason, spelled the
    same way. Slack shipped without it and that was the gap: the session cookie
    carries no `__Host-` prefix, so a compromised sibling subdomain can write
    cookies into the victim's browser, and a state cookie an attacker can plant
    is not a state at all.
    """
    token = read_session_token(request)

    return "" if token is None else hash_session_token(token).hex()


@dataclass(frozen=True, slots=True)
class PendingOAuth:
    """The flow this browser started, as the state cookie recorded it."""

    state: str
    workspace_slug: str
    session: str
    return_path: str


def _format_state_cookie(pending: PendingOAuth) -> str:
    """Serialise the pending flow for the cookie.

    Colon-separated, which is unambiguous rather than lucky: the state is
    `secrets.token_urlsafe` output (base64url, so no colon), and a workspace
    slug is confined to lowercase letters, digits and hyphens by
    `workspaces_slug_format` in migrations/002_tenancy.sql, and the session
    digest is hex. Only the return path could ever contain one, and it is
    last, so a bounded split is exact.
    """
    return (
        f"{pending.state}:{pending.workspace_slug}:"
        f"{pending.session}:{pending.return_path}"
    )


def _parse_state_cookie(raw: str | None) -> PendingOAuth | None:
    """Read back a pending flow, or None if there is not one to read.

    Every malformed shape answers None, and the caller renders all of them as
    the same refusal. The cookie is set by this server and HttpOnly, so a
    malformed one means either no flow was started in this browser or
    something rewrote it -- and neither is a case where telling the caller
    what was wrong with their cookie helps anyone but them.

    The return path is re-validated on the way out even though it was
    validated on the way in. It costs a set membership test, and it means the
    redirect at the end of the flow depends on the allowlist rather than on
    the integrity of a value that spent ten minutes in a browser.
    """
    if not raw:
        return None

    # Bounded, so a return path containing colons stays intact in the last
    # field rather than turning a valid cookie into a malformed one.
    parts = raw.split(":", 3)

    if len(parts) != 4:
        return None

    state, workspace_slug, session, return_path = parts

    if not state or not workspace_slug:
        return None

    return PendingOAuth(
        state=state,
        workspace_slug=workspace_slug,
        session=session,
        return_path=resolve_return_path(return_path),
    )


def _set_state_cookie(
    response: Response,
    *,
    pending: PendingOAuth,
    environment: Environment,
) -> None:
    """Hand the browser the state this flow will be checked against.

    HttpOnly, so a cross-site scripting bug cannot read the state and forge a
    matching callback. Secure only where the deployment speaks https, for the
    reason app/http_cookies.py gives: marking it Secure in development would
    make the browser silently refuse to store it, which reads as an OAuth flow
    that is broken rather than one that is protected.
    """
    response.set_cookie(
        key=OAUTH_STATE_COOKIE_NAME,
        value=_format_state_cookie(pending),
        max_age=OAUTH_STATE_TTL_SECONDS,
        path=OAUTH_STATE_COOKIE_PATH,
        httponly=True,
        secure=is_secure_environment(environment),
        samesite=OAUTH_STATE_COOKIE_SAMESITE,
    )


def _clear_state_cookie(response: Response, *, environment: Environment) -> None:
    """Consume the state. Every attribute must match what set it.

    A delete whose path or flags differ addresses a different cookie, and the
    browser keeps the original -- which would leave a state that has already
    been used still able to match a second callback. That is the single-use
    property, and it is enforced by these two functions reading the same
    constants rather than by anyone remembering to.
    """
    response.delete_cookie(
        key=OAUTH_STATE_COOKIE_NAME,
        path=OAUTH_STATE_COOKIE_PATH,
        httponly=True,
        secure=is_secure_environment(environment),
        samesite=OAUTH_STATE_COOKIE_SAMESITE,
    )


# --- the services these routes need -----------------------------------


@dataclass(frozen=True, slots=True)
class SlackRequestServices:
    """What one Slack request is served with.

    The REST equivalent of app/graphql/context.py's VectorContext, and built
    the same way: a request gets one of each, assembled at the edge, with the
    pool borrowed rather than created.

    The credentials are plain values rather than the Settings object, so that
    a test can construct this without a configured environment -- and so that
    no code path here can reach a setting this dataclass did not deliberately
    expose. `client_secret` is absent for that reason: it is needed only to
    build `oauth`, which is done once in `slack_services`, so no route can
    read it at all.

    All three configuration-dependent fields are optional together. None means
    UNCONFIGURED, and every route below refuses before touching anything else.
    """

    environment: Environment
    client_id: str | None

    # `repr=False`, for the reason SlackGrant.bot_token carries the same
    # marker: this bundle is a local in three request handlers, so any error
    # reporter that captures locals would otherwise print the secret that
    # authenticates every Slack delivery.
    signing_secret: str | None = field(repr=False)

    auth: AuthService
    membership: MembershipService
    slack: SlackService

    oauth: SlackOAuthExchange | None

    # The callback URL registered on the Slack app, or None. Carried here
    # rather than read from settings where it is used, so the authorize leg
    # and the token exchange present one value rather than two.
    #
    # Last and defaulted, because a dataclass field with a default may not
    # precede one without -- and because None is a real state: an app with a
    # single registered callback needs no redirect_uri.
    oauth_callback_url: str | None = None


def slack_services() -> SlackRequestServices:
    """Assemble the per-request services. A FastAPI dependency.

    A dependency rather than module state, so that tests substitute the whole
    bundle through `app.dependency_overrides` -- which is how every test in
    this feature runs with no database, no network and no credentials.

    Settings are resolved here rather than at import, keeping this module
    importable without a configured environment; `get_settings` is lru_cached,
    so it is a dict lookup after the first request.
    """
    settings = get_settings()

    # The pool is owned by the FastAPI lifespan; this only borrows it.
    pool = get_pool()

    client_secret = settings.slack_client_secret
    signing_secret = settings.slack_signing_secret

    oauth: SlackOAuthExchange | None = None

    if settings.slack_client_id is not None and client_secret is not None:
        oauth = SlackOAuthClient(
            client_id=settings.slack_client_id,
            # The one `get_secret_value` in the REST layer, and it goes
            # straight into the client that needs it. The value is never
            # bound to a name this module can reach afterwards.
            client_secret=client_secret.get_secret_value(),
            # The same value the authorize leg sends, so the two cannot
            # disagree -- Slack compares them and refuses the exchange.
            redirect_uri=settings.slack_oauth_callback_url,
        )

    return SlackRequestServices(
        environment=settings.environment,
        client_id=settings.slack_client_id,
        oauth_callback_url=settings.slack_oauth_callback_url,
        signing_secret=(
            signing_secret.get_secret_value() if signing_secret is not None else None
        ),
        auth=AuthService(
            pool=pool,
            users=UserRepository(),
            sessions=SessionRepository(),
            hasher=Argon2PasswordHasher(),
            # Unused on this path -- an OAuth callback only asks the service
            # who the caller already is, and `authenticate` spends no budget --
            # but the service is one object and a half-built one fails at the
            # first call that does need it rather than here.
            rate_limits=RateLimitRepository(),
        ),
        membership=MembershipService(
            pool=pool,
            repository=MembershipRepository(),
            # Unused on this path -- the OAuth callback only asks whether the
            # caller may act in a workspace -- but the service is one object,
            # and a half-built one fails at the first call that does need
            # them rather than here where the mistake was made.
            workspaces=WorkspaceRepository(),
            invitations=InvitationRepository(),
        ),
        slack=SlackService(
            pool=pool,
            repository=SlackRepository(),
            token_store=DatabaseTokenStore(),
            configured=settings.slack_configured,
        ),
        oauth=oauth,
    )


# The dependency as an annotation rather than a default argument.
#
# `Annotated[..., Depends(...)]` and `= Depends(...)` mean the same thing to
# FastAPI, and this spelling is the one that is also true: a default argument
# that is a function call is evaluated once at import, which is a real bug in
# every other context and only harmless here because FastAPI reads the marker
# rather than the value. The annotation keeps the parameter genuinely
# required, and keeps flake8-bugbear from having to be told to ignore it.
SlackServices = Annotated[SlackRequestServices, Depends(slack_services)]


# The three refusals these routes share, as functions so the wording and the
# status code are decided once. None of them says anything about the server:
# "not configured" is a deployment fact an admin already knows, and the other
# two describe the request.
def _unconfigured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Slack is not configured",
    )


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )


def _not_found() -> HTTPException:
    """One answer for a slug that matches nothing, one the caller is not in,
    and one they are in but may not administer.

    The same collapse `MembershipQuery.my_workspace` makes, for the same
    reason and one more: distinguishing the third case would tell any member
    of any workspace that this deployment has a Slack app configured.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Workspace not found",
    )


async def _authorized_admin_scope(
    request: Request,
    services: SlackRequestServices,
    slug: str,
) -> AuthorizedWorkspaceScope:
    """Resolve the caller to an admin of this workspace, or refuse.

    Fails closed and fails first, mirroring app/graphql/viewer.py: the viewer
    comes from the session cookie the request actually presented, and no
    protected lookup happens for a caller without one.

    The workspace slug reaches exactly two frames -- this one and the
    membership lookup it delegates to. Past this point the flow holds a scope.
    """
    viewer = await services.auth.authenticate(read_session_token(request))

    if viewer is None:
        raise _unauthenticated()

    try:
        scope = await services.membership.authorized_scope_for_slug(
            slug=slug,
            user_id=viewer.id,
        )
    except WorkspaceAccessDeniedError:
        raise _not_found() from None

    try:
        services.slack.require_admin(scope)
    except WorkspaceAccessDeniedError:
        raise _not_found() from None

    return scope


# --- the routes -------------------------------------------------------


@router.get("/oauth/start")
async def slack_oauth_start(
    request: Request,
    services: SlackServices,
    workspace: str,
    return_to: str | None = None,
) -> Response:
    """Begin an installation: mint a state, then send the admin to Slack.

    The state is generated here and stored in a cookie, which is what binds it
    to this browser. It is not stored in the database and does not need to be:
    the only question the callback asks is "did the browser presenting this
    state receive it from me", and a cookie answers exactly that, without a
    table that would then need expiring and pruning.

    Authorization happens BEFORE a state is issued. A caller who may not
    administer this workspace gets no state, so there is nothing for them to
    carry through Slack's consent screen and back.

    And the state is issued on the CALLBACK's origin. A cookie set on the
    host the browser happens to be on is not sent to the host Slack redirects
    to, so a flow started from the dev origin came back to a callback holding
    no state and was refused -- correctly, and for a reason nothing in the
    callback could fix. See app/rest/oauth_origin.py.
    """
    if services.client_id is None:
        raise _unconfigured()

    # Before authorization, so the session checked is the one on the origin
    # the flow will finish on. A no-op wherever the app is served from its
    # own callback origin, which is every deployment that is not tunnelled.
    elsewhere = redirect_to_callback_origin(request, services.oauth_callback_url)

    if elsewhere is not None:
        return elsewhere

    # Called for its refusal, not its value. The scope is not carried into the
    # cookie or the redirect: the callback re-authorizes from the session
    # rather than trusting anything that spent ten minutes in a browser.
    await _authorized_admin_scope(request, services, workspace)

    state = secrets.token_urlsafe(STATE_ENTROPY_BYTES)

    response = RedirectResponse(
        authorize_url(
            client_id=services.client_id,
            state=state,
            redirect_uri=services.oauth_callback_url,
        ),
        # 303, so the browser follows with GET whatever this request was.
        status_code=status.HTTP_303_SEE_OTHER,
    )

    _set_state_cookie(
        response,
        pending=PendingOAuth(
            state=state,
            # The slug the caller named, recorded only after it resolved to a
            # workspace this admin holds. `scope` is what proves that; it is
            # not carried in the cookie because the callback re-authorizes
            # from scratch rather than trusting anything a browser held.
            workspace_slug=workspace,
            # Binds the state to the browser session that started the flow,
            # exactly as app/rest/github.py does. Without it the cookie is
            # something an attacker who can write cookies into the victim's
            # browser -- a compromised sibling subdomain, since the session
            # cookie carries no __Host- prefix -- can plant alongside a
            # matching `?state=` and their own `code`, completing an install
            # into the victim's workspace. Forging this too would require the
            # victim's session token, which is HttpOnly and never leaves the
            # browser.
            session=_session_digest(request),
            return_path=resolve_return_path(return_to),
        ),
        environment=services.environment,
    )

    return response


@router.get("/oauth/callback")
async def slack_oauth_callback(
    request: Request,
    services: SlackServices,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> Response:
    """Finish an installation Slack has just approved.

    Order matters here and is the security of the endpoint.

    The state is checked FIRST, before the session is read and before the code
    is touched. An unvalidated state is CSRF: without it, anyone can send an
    admin's browser to this URL with a code from an attacker-controlled Slack
    workspace, and the admin's own session silently connects the attacker's
    Slack to the admin's workspace -- an integration that then receives that
    workspace's messages.

    The state is consumed the instant it matches. Every path after that point
    clears the cookie, so one issued state completes at most one flow; a
    replay of the identical URL finds no cookie and is refused. A state that
    did NOT match deliberately leaves the cookie alone -- otherwise anyone who
    could send the admin's browser one forged callback could cancel a
    legitimate flow in progress.

    Authorization is redone from the session rather than read from the cookie.
    The cookie says which workspace the flow was started for; that it may be
    connected is a question only `workspace_members` answers, and it is asked
    again here in case membership changed while the admin was on Slack.
    """
    if services.oauth is None:
        raise _unconfigured()

    pending = _parse_state_cookie(request.cookies.get(OAUTH_STATE_COOKIE_NAME))

    if pending is None or state is None:
        raise _state_rejected()

    if not hmac.compare_digest(
        state.encode("utf-8"),
        pending.state.encode("utf-8"),
    ):
        raise _state_rejected()

    # The state must be finished under the session that started it. Checked
    # with the same refusal as a wrong state, so a planted cookie and a forged
    # state are one answer, and an empty digest -- a request with no session --
    # matches nothing a real flow ever wrote.
    if not hmac.compare_digest(
        _session_digest(request).encode("utf-8"),
        pending.session.encode("utf-8"),
    ):
        raise _state_rejected()

    scope = await _authorized_admin_scope(request, services, pending.workspace_slug)

    # From here the state has been consumed, whatever happens next.
    def consumed(response: Response) -> Response:
        _clear_state_cookie(response, environment=services.environment)

        return response

    if error is not None or code is None:
        # Slack sends `error=access_denied` when an admin declines, and that
        # is not a failure of this server -- so the admin is returned to the
        # product rather than shown an error page. The value is not echoed
        # anywhere: it is a string from a query parameter.
        return consumed(
            _redirect_to(pending.return_path, workspace_slug=pending.workspace_slug)
        )

    try:
        grant = await services.oauth.exchange(code=code)
    except SlackOAuthError:
        return consumed(
            _problem(
                status.HTTP_400_BAD_REQUEST,
                "Slack did not accept the authorization code",
            )
        )

    try:
        await services.slack.connect(scope=scope, grant=grant)
    except SlackTeamAlreadyConnectedError:
        return consumed(
            _problem(
                status.HTTP_409_CONFLICT,
                "That Slack workspace is already connected",
            )
        )

    return consumed(
        _redirect_to(pending.return_path, workspace_slug=pending.workspace_slug)
    )


@router.post("/events")
async def slack_events(
    request: Request,
    services: SlackServices,
) -> Response:
    """Slack's event feed. Verified, deduplicated, and loop-safe.

    Answers 200 for everything it accepts, including events it deliberately
    ignores. That is Slack's contract rather than laziness: any non-2xx is a
    retry, so reporting "I chose not to act on this" as an error would have
    Slack send it three more times and then disable the endpoint.

    Nothing is ingested yet -- this is the foundation, and the step that turns
    a message into a Vector object lands with the feature that needs it. What
    is here is everything that must be right BEFORE that step exists, because
    adding it later to an endpoint without these checks is how an integration
    ships that trusts unsigned requests and answers its own messages.
    """
    if services.signing_secret is None:
        raise _unconfigured()

    # Read before anything else touches the request, and used as bytes
    # throughout. `await request.json()` here would be the bug the whole
    # scheme is about: it parses an unverified body, and it leaves the
    # signature to be checked against a re-encoding of the result.
    body = await request.body()

    if not verify_slack_signature(
        signing_secret=services.signing_secret,
        timestamp=request.headers.get(TIMESTAMP_HEADER),
        signature=request.headers.get(SIGNATURE_HEADER),
        body=body,
        now=time.time(),
    ):
        # One answer for a missing header, a wrong secret, a tampered body and
        # a stale timestamp. See verify_slack_signature for why the caller
        # must not learn which.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid request signature",
        )

    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed payload",
        ) from None

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed payload",
        )

    if payload.get("type") == "url_verification":
        # The handshake Slack performs once, when the URL is first saved. It
        # is signed like every other delivery, so it is answered here rather
        # than before verification -- an unsigned handshake is somebody else
        # asking whether this endpoint exists.
        challenge = payload.get("challenge")

        if not isinstance(challenge, str):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Malformed payload",
            )

        return JSONResponse({"challenge": challenge})

    if payload.get("type") != "event_callback":
        return _acknowledged()

    return await _handle_event_callback(payload, services)


async def _handle_event_callback(
    payload: dict[str, Any],
    services: SlackRequestServices,
) -> Response:
    """Route one verified event, or decide not to.

    The order of the three gates is deliberate.

    The installation lookup is first, because an event for a Slack workspace
    this deployment has disconnected has nowhere to go and nothing to write.

    The loop check is second, before the deduplication write. A busy workspace
    where Vector posts often would otherwise pay one INSERT per message it
    sent itself, on the endpoint's hottest path, to record an event that is
    then thrown away.

    The claim is last, so the only events that occupy a row in the ledger are
    the ones that were going to be acted on.
    """
    event = payload.get("event")
    event_id = payload.get("event_id")
    team_id = payload.get("team_id")

    if not isinstance(event, dict) or not isinstance(event_id, str) or not event_id:
        return _acknowledged()

    if not isinstance(team_id, str) or not team_id:
        return _acknowledged()

    installation = await services.slack.installation_for_team(slack_team_id=team_id)

    if installation is None:
        return _acknowledged()

    if is_own_event(event, bot_user_id=installation.bot_user_id):
        return _acknowledged()

    if not await services.slack.claim_event(event_id=event_id):
        # A redelivery. Slack retries three times on any non-2xx and again
        # during its own incidents, so this is an ordinary event rather than
        # an anomaly, and the correct response is a plain acknowledgement.
        return _acknowledged()

    # Where ingestion goes. Deliberately empty: everything above is the part
    # that has to exist before anything here can be written safely.
    return _acknowledged()


def is_own_event(event: dict[str, Any], *, bot_user_id: str) -> bool:
    """Whether this event is one Vector, or another app, caused.

    Loop prevention, and the reason it is not optional: Vector posts into
    Slack, Slack delivers Vector's own message back as an event, and an
    integration that acts on it acts on its own output. If acting means
    posting -- a confirmation, a summary, a link -- then each pass produces
    another event, and the loop runs at Slack's rate limit until a human
    notices. There is no natural stopping point, because every message in the
    loop is genuinely new.

    Three signals, because Slack marks bot traffic three different ways
    depending on how it was posted and how old the API surface is:

    * `bot_id` -- present on any message posted by any app, including ours.
    * `subtype == "bot_message"` -- the older shape, still delivered.
    * `user == bot_user_id` -- our bot acting as itself, which is what
      `chat.postMessage` with a bot token produces.

    All three, not the last one alone: an event from a DIFFERENT app is not
    ours, but acting on it can still complete a loop through that app, and
    Vector has no way to know what the other app does with what it posts.
    Ignoring every bot is the version that cannot be wrong.
    """
    return (
        event.get("bot_id") is not None
        or event.get("subtype") == "bot_message"
        or event.get("user") == bot_user_id
    )


# --- small response helpers -------------------------------------------


def _acknowledged() -> Response:
    """200, meaning "received". Never a comment on what was done with it."""
    return JSONResponse({"ok": True})


def _redirect_to(template: str, *, workspace_slug: str) -> Response:
    """A redirect to an allowlisted path. The only redirect this module makes.

    Takes a template that has already been through `resolve_return_path`, and
    the workspace the state cookie recorded. There is no route from a request
    parameter to this function that skips either: the template comes from a
    fixed set and the slug from the cookie this server wrote.

    The slug is quoted before substitution. It is confined to lowercase
    letters, digits and hyphens by `workspaces_slug_format` in
    migrations/002_tenancy.sql, so quoting changes nothing today -- and is
    still where the rule belongs, because a Location header built by
    concatenation is how an open redirect is introduced later.
    """
    return RedirectResponse(
        template.format(slug=quote(workspace_slug, safe="")),
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _problem(status_code: int, detail: str) -> Response:
    """A failure rendered as a response rather than raised.

    The callback needs this: an HTTPException unwinds past the code that
    clears the state cookie, so a raised failure would leave a consumed state
    still valid in the browser.
    """
    return JSONResponse({"detail": detail}, status_code=status_code)


def _state_rejected() -> HTTPException:
    """The one answer for every way a state fails to validate.

    Missing cookie, missing parameter, malformed cookie, mismatch, and replay
    all read the same. A caller who could tell them apart could tell whether a
    flow is in progress in the browser they are attacking.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid OAuth state",
    )
