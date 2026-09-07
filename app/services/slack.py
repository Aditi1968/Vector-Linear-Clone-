"""Slack integration rules, and the two seams that keep secrets out of them.

The service is ordinary: it owns connection acquisition, transaction
boundaries and every authorization decision about the integration. The two
things worth reading first are the seams either side of it -- SlackTokenStore,
which is where a bot token lives, and SlackOAuthClient, which is the only code
here that talks to Slack over the network. Both are protocols with one
implementation each, so tests substitute them and neither the service nor its
tests ever need a credential.
"""

import asyncio
import json
import urllib.parse
import urllib.request
from typing import Any, Protocol
from uuid import UUID

import asyncpg
import httpx

from app.domain.errors import (
    ValidationError,
    ValidationIssue,
    WorkspaceAccessDeniedError,
)
from app.domain.slack import (
    CHANNEL_UNAVAILABLE_CODES,
    SCOPE_LIST_CHANNELS,
    SCOPE_POST_MESSAGE,
    SLACK_ADMIN_ROLES,
    SLACK_NOTIFICATION_EVENTS,
    SlackApiError,
    SlackChannelEntity,
    SlackChannelSync,
    SlackDeliveryResult,
    SlackGrant,
    SlackInstallationEntity,
    SlackIntegrationView,
    SlackNotificationPreference,
    SlackNotificationSettingsEntity,
    SlackOAuthError,
    SlackTeamAlreadyConnectedError,
)
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.repositories.slack import (
    SLACK_INSTALLATIONS_TEAM_KEY,
    SlackRepository,
)


# Slack's OAuth v2 endpoints. Constants rather than settings: they are
# properties of Slack, not of a deployment, and a configurable authorize URL
# is an open redirect with extra steps -- whoever could set it could point
# every admin's browser at a page of their choosing that looks exactly like
# the real consent screen.
SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_ACCESS_URL = "https://slack.com/api/oauth.v2.access"

# What Vector asks a workspace for. The minimum that a message-driven
# integration needs and nothing beyond it: `chat:write` to post, and
# `channels:read` to let an admin pick a channel. Asking for more up front is
# how an integration gets declined by a security-conscious admin, and every
# extra scope is a wider blast radius for the token this file stores.
#
# What is REQUESTED is not what is GRANTED -- an admin can decline
# individually -- so nothing may read this tuple to decide whether a feature
# is permitted. `slack_installations.scopes` is what Slack actually agreed to,
# and it is the only thing a permission check may consult.
REQUESTED_SCOPES = ("channels:read", "chat:write")

# Bound on the one outbound HTTP call this integration makes. It runs inside
# an admin's OAuth callback, so an unbounded wait is a browser hanging on a
# redirect that never lands; and it runs in a worker thread, so nothing here
# can cancel it once started -- which is exactly why the bound has to be
# passed to the socket rather than wrapped around the await.
OAUTH_EXCHANGE_TIMEOUT_SECONDS = 10.0

# The two Web API methods this feature calls, and the only two. Constants for
# the reason the OAuth endpoints above are constants: they are properties of
# Slack rather than of a deployment, and a configurable API host is a way to
# send every workspace's bot token somewhere else.
SLACK_CONVERSATIONS_LIST_URL = "https://slack.com/api/conversations.list"
SLACK_CHAT_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

# Bounds on the channel listing.
#
# 200 is Slack's own recommended page size for `conversations.list`, and ten
# pages is where the crawl stops. Both numbers matter: a workspace with tens of
# thousands of channels would otherwise turn one admin pressing "refresh" into
# a minutes-long loop against a rate-limited endpoint, holding an HTTP request
# open the whole time. Stopping early yields a partial list, which is a
# picker missing some channels -- worse than complete, better than a request
# that never returns, and the honest fix when someone hits it is a server-side
# name search rather than a longer crawl.
CHANNEL_PAGE_SIZE = 200
MAX_CHANNEL_PAGES = 10

# How long the Web API may take. Both calls sit inside a request an admin is
# watching, so the bound is the same short one the OAuth exchange uses.
WEB_API_TIMEOUT_SECONDS = 10.0

# What a test notification says.
#
# Deliberately dull, and deliberately not templated from anything a client
# sent: this message is posted into a company's Slack by pressing a button, so
# the one thing it must not be is a way to make Vector say arbitrary words in
# somebody's channel.
TEST_MESSAGE_TEXT = (
    "Vector is connected to this channel. "
    "You'll see notifications here for the events you've enabled."
)


class SlackTokenStore(Protocol):
    """Where a bot token lives, and how a stored row points back at it.

    The interface exists so that the fix for the current storage is a swap
    rather than a schema change, and so it has exactly the shape a KMS-backed
    store needs: `store` takes a secret and hands back an opaque reference the
    row can hold, `fetch` turns that reference back into the secret, and
    `backend` names which store issued it so a row written by one store is
    never handed to another.

    Both methods are async even though the implementation below does no I/O.
    That is deliberate: a secret manager is a network call, and a synchronous
    protocol would mean every call site changes on the day the store does --
    which is the ripple the abstraction is here to avoid.
    """

    @property
    def backend(self) -> str: ...

    async def store(self, *, workspace_id: UUID, token: str) -> str: ...

    async def fetch(self, *, workspace_id: UUID, reference: str) -> str: ...


class DatabaseTokenStore:
    """The token, in the database column, in plaintext.

    Be precise about what this does and does not give, because the interface
    above makes it easy to read protection into a design that has none yet.

    What it gives: the token is written and read through one named seam, so
    there is exactly one place to change; the column is never in a SELECT list
    beside the rest of the installation, so a status query cannot carry it;
    and no entity, GraphQL type or log line in this feature holds it.

    What it does NOT give: any encryption at rest beyond whatever the storage
    layer provides underneath PostgreSQL, and any protection at all from
    someone who can read the table. A backup, a restored snapshot, a support
    query or a `SELECT *` in a slow-query log yields a live credential that can
    post as Vector into every channel the bot has joined, and the only
    revocation is Slack's.

    Hashing is not an option here and the contrast with session and invitation
    tokens is worth stating: those are secrets a client presents and the server
    only has to recognise, so a digest suffices. This is a secret the server
    must present to Slack, so it has to be recoverable, and the honest fix is a
    store that keeps it somewhere PostgreSQL is not -- a KMS, a secret manager
    -- and writes an ARN or key id here instead.

    The reference IS the token, which is why `store` and `fetch` are identity
    functions. `workspace_id` is unused by this implementation and is on the
    protocol because a real store keys, scopes and audits by tenant.
    """

    @property
    def backend(self) -> str:
        return "database"

    async def store(self, *, workspace_id: UUID, token: str) -> str:
        return token

    async def fetch(self, *, workspace_id: UUID, reference: str) -> str:
        return reference


class SlackOAuthExchange(Protocol):
    """Turns an authorization code into a grant. One network call, isolated.

    A protocol so that every test in this feature substitutes it. That is not
    only about avoiding the network: there are no Slack credentials in this
    environment, so a test that reached the real client could not be written
    at all, and the alternative -- an integration whose OAuth path is only
    exercised by hand against a live app -- is the path that is never tested.
    """

    async def exchange(self, *, code: str) -> SlackGrant: ...


class SlackOAuthClient:
    """`oauth.v2.access` over HTTPS, using the standard library.

    urllib rather than an async HTTP client, because there is no async client
    in the runtime dependency set and one outbound call on the rarest path in
    the product does not justify adding one. The blocking call runs in a
    worker thread, so the event loop is never held.

    The credentials are constructor arguments and are never logged, never
    attached to an exception, and never returned. Slack's own error responses
    can echo the submitted code, so the response body is read for the fields
    this class needs and is otherwise discarded rather than raised or logged.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    async def exchange(self, *, code: str) -> SlackGrant:
        fields = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
        }

        # Presented again here because Slack compares it with the value the
        # authorization began with and refuses the exchange when they differ.
        # Sent only when one was sent on the first leg, so the two legs cannot
        # disagree about whether there is a redirect_uri at all.
        if self._redirect_uri:
            fields["redirect_uri"] = self._redirect_uri

        payload = await asyncio.to_thread(_post_form, SLACK_ACCESS_URL, fields)

        return _grant_from_response(payload)


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    """POST a form and decode the JSON answer. Blocking; call in a thread.

    A module function rather than a method so that nothing about the request
    depends on instance state that a reader has to go and find. The URL is a
    module constant and never an argument from outside this file, which is
    what keeps this from being a general-purpose fetcher pointed by a caller.
    """
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("utf-8"),
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=OAUTH_EXCHANGE_TIMEOUT_SECONDS,
    ) as response:
        decoded = json.loads(response.read())

    if not isinstance(decoded, dict):
        raise SlackOAuthError()

    return decoded


def _non_empty_text(value: Any) -> str | None:
    """`value` if it is a non-empty string, else None.

    Empty counts as absent on purpose. A `bot_user_id` of "" is JSON that
    parses and a comparison that never matches, so accepting it would leave
    the loop prevention in app/rest/slack.py silently disabled -- the failure
    would be an integration that answers its own messages, forever, with
    nothing in any log to say why.
    """
    return value if isinstance(value, str) and value else None


def _grant_from_response(payload: dict[str, Any]) -> SlackGrant:
    """Read a grant out of Slack's answer, or refuse the whole thing.

    Slack answers HTTP 200 with `{"ok": false, "error": "..."}` for a refused
    exchange, so the status code says nothing and `ok` is the only signal.

    Every field is then read defensively, because this is a third party's JSON
    and every value here has a job that a plausible default would quietly
    break. Refusing the whole response is the only safe answer: a half-read
    grant would be stored as an installation that cannot post, cannot be told
    apart from a working one, and can only be fixed by an admin who first has
    to work out that it is broken.

    `scope` arrives comma-joined and is split here, at the boundary, so that
    everything inward of this function holds a real list.
    """
    if payload.get("ok") is not True:
        raise SlackOAuthError()

    team = payload.get("team")

    if not isinstance(team, dict):
        raise SlackOAuthError()

    token = _non_empty_text(payload.get("access_token"))
    bot_user_id = _non_empty_text(payload.get("bot_user_id"))
    scope = _non_empty_text(payload.get("scope"))
    team_id = _non_empty_text(team.get("id"))
    team_name = _non_empty_text(team.get("name"))

    if (
        token is None
        or bot_user_id is None
        or scope is None
        or team_id is None
        or team_name is None
    ):
        raise SlackOAuthError()

    return SlackGrant(
        slack_team_id=team_id,
        slack_team_name=team_name,
        bot_user_id=bot_user_id,
        scopes=tuple(part for part in scope.split(",") if part),
        bot_token=token,
    )


class SlackWebApi(Protocol):
    """The two Slack Web API calls this feature makes, behind one seam.

    A protocol for the reason SlackOAuthExchange is one: there are no Slack
    credentials in this environment, so a test that reached the real client
    could not be written -- and the alternative is a feature whose network path
    is only ever exercised by hand against a live workspace.

    Both methods take the bot token as an argument rather than holding one.
    The implementation is therefore stateless and shared, and -- more to the
    point -- no long-lived object in this process has a credential on it for a
    traceback or an error reporter to print. The token is a local in the frame
    that needs it and nowhere else.

    Both raise SlackApiError rather than returning a status. Slack answers HTTP
    200 for a refusal, so there is no status code to return; and a caller that
    could receive a failure as a value is a caller that can forget to look at
    it, which on the test-notification path means reporting a delivery that did
    not happen.
    """

    async def conversations_list(self, *, token: str) -> list[SlackChannelEntity]: ...

    async def post_message(self, *, token: str, channel: str, text: str) -> None: ...


class SlackWebClient:
    """`conversations.list` and `chat.postMessage` over HTTPS.

    httpx rather than the urllib-in-a-thread the OAuth client uses, because
    httpx is already a runtime dependency (app/services/github.py uses it) and
    because the listing is a paged crawl -- several round trips under one
    timeout, which is exactly the thing a blocking call in a worker thread
    handles badly.

    The token is a parameter on every method and is never stored on the
    instance, never logged, and never attached to a raised exception. Slack's
    error responses do not echo it, but the request headers hold it, so nothing
    here reads or reports a request object.
    """

    async def conversations_list(self, *, token: str) -> list[SlackChannelEntity]:
        """Every public channel this bot token can see, as far as the bound.

        `types=public_channel` and nothing else, because `channels:read` is
        what the grant carries. Asking for `private_channel` as well would
        change the failure from "an empty extra section" to `missing_scope` on
        the whole call, so a request for data we are not permitted to read
        would take the data we are with it.

        `exclude_archived=false`, deliberately. An archived channel is one an
        admin may already have chosen, and hiding it from the sync would mark
        it inaccessible and leave the settings screen unable to explain why the
        notifications stopped. It arrives flagged instead.

        Paged through `response_metadata.next_cursor`, which Slack sends as an
        empty string on the last page -- so the loop tests for truthiness
        rather than for the key being present. Bounded by MAX_CHANNEL_PAGES;
        see the note there for why a partial answer beats an unbounded crawl.

        One AsyncClient for the whole crawl, so the connection is reused across
        pages, and one timeout across all of them.
        """
        channels: list[SlackChannelEntity] = []
        cursor = ""

        async with httpx.AsyncClient(timeout=WEB_API_TIMEOUT_SECONDS) as client:
            for _ in range(MAX_CHANNEL_PAGES):
                parameters = {
                    "types": "public_channel",
                    "exclude_archived": "false",
                    "limit": str(CHANNEL_PAGE_SIZE),
                }

                if cursor:
                    parameters["cursor"] = cursor

                payload = await _web_call(
                    client,
                    "GET",
                    SLACK_CONVERSATIONS_LIST_URL,
                    token=token,
                    params=parameters,
                )

                channels.extend(_channels_from_response(payload))

                metadata = payload.get("response_metadata")
                following = (
                    metadata.get("next_cursor") if isinstance(metadata, dict) else None
                )

                # Its own name rather than reassigning `cursor`, so the
                # narrowing below happens before anything the next request
                # would read. Slack sends an EMPTY string on the last page
                # rather than omitting the key, so testing for the key's
                # presence would page until MAX_CHANNEL_PAGES ran out --
                # silently truncating every large workspace's list.
                if not isinstance(following, str) or not following:
                    break

                cursor = following

        return channels

    async def post_message(self, *, token: str, channel: str, text: str) -> None:
        """Post one message, or raise.

        Returns None on success rather than the message's timestamp. Nothing in
        this feature threads or updates a message it posted, and returning an
        identifier nobody stores would be inviting a caller to store it
        somewhere this schema has no column for.

        JSON rather than a form body: `chat.postMessage` accepts both, and JSON
        is the one where a text containing an ampersand or a newline is a value
        rather than something an encoder has to get right.
        """
        async with httpx.AsyncClient(timeout=WEB_API_TIMEOUT_SECONDS) as client:
            await _web_call(
                client,
                "POST",
                SLACK_CHAT_POST_MESSAGE_URL,
                token=token,
                json={"channel": channel, "text": text},
            )


async def _web_call(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    token: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """One Slack Web API request, decoded, or SlackApiError.

    The three failure shapes are collapsed into one exception with different
    payloads, because the caller's question is always "may I report success"
    and the answer is no for all three -- but the REASON differs and only Slack
    knows two of them:

    * a transport failure or a body that is not a JSON object raises with
      `code=None`, which the service reads as "we never found out";
    * a non-2xx status raises with `code=None` as well. Slack signals refusals
      through the body at HTTP 200, so a 5xx or a 429 is Slack being unwell
      rather than Slack disagreeing;
    * `{"ok": false, "error": "..."}` raises carrying Slack's own word for it,
      which is the only case anything downstream can act on.

    The response body is read for these fields and otherwise discarded. It is a
    third party's JSON, it is not logged, and it is never attached to an
    exception beyond the one bounded `error` string.
    """
    try:
        response = await client.request(
            method,
            url,
            headers={"Authorization": f"Bearer {token}"},
            **kwargs,
        )
    except httpx.HTTPError:
        # Caught as a family rather than by class: httpx raises a dozen
        # different transport errors and every one of them means the same
        # thing here, which is that no answer arrived.
        raise SlackApiError() from None

    if response.status_code != 200:
        raise SlackApiError()

    try:
        payload = response.json()
    except ValueError:
        raise SlackApiError() from None

    if not isinstance(payload, dict):
        raise SlackApiError()

    if payload.get("ok") is not True:
        # `is not True` rather than falsiness, matching `_grant_from_response`:
        # a string "true" is not an acknowledgement, and treating it as one
        # would report a delivery that did not happen.
        error = payload.get("error")

        raise SlackApiError(error if isinstance(error, str) and error else None)

    return payload


def _channels_from_response(payload: dict[str, Any]) -> list[SlackChannelEntity]:
    """Read the channels out of one page, skipping anything unusable.

    Skipping rather than refusing the whole page, which is the opposite of what
    `_grant_from_response` does with an OAuth grant -- and the difference is
    what the answer is for. A half-read grant becomes an installation that
    cannot post and cannot be told from a working one, so it must be refused. A
    channel missing its name is one row of a picker; dropping it costs an admin
    one entry in a list, while refusing the page costs them the whole feature
    because one channel in their workspace has a shape this version did not
    expect.

    `is_private` is read from the payload rather than assumed FALSE from the
    `types=public_channel` request. The request is what we asked for; this is
    what Slack said, and storing the second is what keeps the row honest if the
    request ever changes.
    """
    listed = payload.get("channels")

    if not isinstance(listed, list):
        return []

    channels: list[SlackChannelEntity] = []

    for entry in listed:
        if not isinstance(entry, dict):
            continue

        channel_id = _non_empty_text(entry.get("id"))
        name = _non_empty_text(entry.get("name"))

        if channel_id is None or name is None:
            continue

        channels.append(
            SlackChannelEntity(
                channel_id=channel_id,
                # Slack's ceiling is 80 characters and
                # `slack_channels_name_length` enforces the same, so a longer
                # name is a response this version does not understand -- but it
                # would abort the whole sync on a CHECK violation, which is a
                # 500 for one odd channel. Truncated instead, at the boundary,
                # where the decision is visible.
                name=name[:80],
                is_private=entry.get("is_private") is True,
                is_archived=entry.get("is_archived") is True,
                is_member=entry.get("is_member") is True,
                # Just listed, so accessible by definition. The column exists
                # for the channels that are NOT in this response.
                is_accessible=True,
            )
        )

    return channels


def _failure_for(error: SlackApiError) -> str:
    """Translate one Slack refusal into this feature's own vocabulary.

    The single frame that knows Slack's error strings, which is what keeps
    SLACK_FAILURES a set of our words rather than a provider's. Anything
    unrecognised is SLACK_REFUSED -- honest about the fact that Slack said no
    and that this version cannot say why, rather than guessing at a cause and
    sending an admin to fix the wrong thing.
    """
    if error.code is None:
        return "slack_unreachable"

    if error.code in CHANNEL_UNAVAILABLE_CODES:
        return "channel_unavailable"

    if error.code == "missing_scope":
        return "missing_scope"

    if error.code in ("invalid_auth", "token_revoked", "account_inactive"):
        # The stored token no longer works at Slack. To an admin that is the
        # same situation as never having connected -- the fix is the connect
        # button -- so it is reported as the state they can act on rather than
        # as an opaque refusal.
        return "not_connected"

    return "slack_refused"


async def fetch_token(
    store: SlackTokenStore,
    stored: tuple[str, str],
    *,
    workspace_id: UUID,
) -> str:
    """Turn a stored (backend, reference) pair into the live bot token.

    One function so that every path presenting a credential to Slack cannot
    end up with a different opinion about a row written by a store this process
    does not have. Without the check the reference would be handed back as if
    it were the secret and sent to Slack as an Authorization header -- a
    failure that looks like an expired token and is actually a store mismatch.

    A module function rather than a method, because there are now two callers
    with nothing else in common: `SlackService`, which reaches it on behalf of
    an admin who has been authorized, and the delivery loop in
    `app.services.notifications`, which runs on no request and has no viewer to
    authorize. Neither is entitled to its own copy of this check.

    It performs NO authorization of its own, and must not be read as a
    boundary. `SlackService.bot_token` does that with `require_admin`; the
    delivery loop's equivalent is that the workspace it passes came out of a
    row the database matched, not out of anything a client sent.

    Returns the token and never logs, stores or attaches it to an exception.
    """
    backend, reference = stored

    if backend != store.backend:
        raise RuntimeError(
            f"stored Slack token uses the {backend!r} store; "
            f"this process is configured with {store.backend!r}"
        )

    return await store.fetch(workspace_id=workspace_id, reference=reference)


def authorize_url(
    *, client_id: str, state: str, redirect_uri: str | None = None
) -> str:
    """Where an admin's browser is sent to approve the installation.

    Built here rather than in the REST layer so the requested scopes and the
    endpoint are decided in one place, next to the note about what a granted
    scope is. `state` is quoted along with everything else by urlencode, which
    is what stops a state value from being able to add parameters of its own.

    `redirect_uri` is sent when the deployment configured one, and omitting
    it was a real failure rather than a simplification: Slack answered
    "redirect_uri did not match any configured URIs. Passed URI:" with nothing
    after the colon, because it will only infer the callback for an app that
    has exactly one and treats an absent value as a mismatch otherwise. The
    same value has to be presented again at the token exchange -- Slack
    compares the two and refuses the exchange when they differ -- which is why
    it is threaded through rather than rebuilt in each place.
    """
    parameters = {
        "client_id": client_id,
        "scope": ",".join(REQUESTED_SCOPES),
        "state": state,
    }

    if redirect_uri:
        parameters["redirect_uri"] = redirect_uri

    query = urllib.parse.urlencode(parameters)

    return f"{SLACK_AUTHORIZE_URL}?{query}"


class SlackService:
    """Business rules for the Slack integration.

    Every authorization decision about the integration is made here, and
    `require_admin` is the only place that reads a role. Both transports --
    the GraphQL resolvers and the OAuth callback -- go through this class, so
    "may this caller see or change the integration" has one answer rather than
    one per transport.

    Nothing here trusts an argument to describe the caller. Every method that
    acts on a workspace takes an AuthorizedWorkspaceScope, which can only be
    built from a row in `workspace_members`; a plain WorkspaceScope will not
    satisfy the annotation. The two methods that do not take one --
    `installation_for_team` and `claim_event` -- are the webhook path, where
    the caller is Slack and the evidence is a verified request signature
    rather than a membership row. They are deliberately the only two, and
    neither reads or writes anything a client could have named.

    `configured` is passed in rather than read from settings, so that this
    class can be exercised in both states without touching the environment,
    and so that one request cannot answer one field as configured and another
    as not.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: SlackRepository,
        token_store: SlackTokenStore,
        configured: bool,
        web: SlackWebApi | None = None,
    ):
        self._pool = pool
        self._repository = repository
        self._token_store = token_store
        self._configured = configured

        # Optional, and None is not a silent state. Every method that would
        # reach the network goes through `_web_api`, which raises if this slot
        # was not filled -- so a composition root that forgot fails loudly on
        # the first sync rather than reporting an empty channel list as an
        # answer. Defaulted so the paths that never touch Slack over the
        # network -- the OAuth callback, the status field, disconnect -- can
        # build this service without one.
        self._web = web

    async def status(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
    ) -> SlackIntegrationView:
        """What this workspace's admin should be shown.

        An unconfigured deployment answers without a connection, and that is a
        property rather than an optimisation: with no Slack app there is
        nothing a row could mean, so asking the database would be reading
        state the product cannot act on. It also keeps the status field cheap
        on every deployment that never connects Slack, which is most of them.
        """
        self.require_admin(scope)

        if not self._configured:
            # Returned without acquiring a connection, which is the property
            # worth keeping separate from the guard inside `_view`: on a
            # deployment with no Slack app this field must not be a query on
            # every settings page load.
            return self._view(None)

        async with self._pool.acquire() as connection:
            installation = await self._repository.find(
                connection,
                workspace_id=scope.workspace_id,
            )

        return self._view(installation)

    async def connect(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        grant: SlackGrant,
    ) -> SlackIntegrationView:
        """Store a completed grant as this workspace's installation.

        The token goes to the store first and only its reference reaches the
        INSERT, so the one statement that could put a credential in a query
        log never sees one.

        `connected_by_user_id` comes from the scope, not from an argument.
        The scope was built from the `workspace_members` row that authorized
        this call, so the recorded connector is the account the database
        matched -- there is no parameter here for a caller to name somebody
        else in.
        """
        self.require_admin(scope)

        reference = await self._token_store.store(
            workspace_id=scope.workspace_id,
            token=grant.bot_token,
        )

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block will
            # also carry the audit write that records who connected what.
            async with connection.transaction():
                try:
                    installation = await self._repository.upsert(
                        connection,
                        workspace_id=scope.workspace_id,
                        slack_team_id=grant.slack_team_id,
                        slack_team_name=grant.slack_team_name,
                        bot_user_id=grant.bot_user_id,
                        scopes=list(grant.scopes),
                        token_backend=self._token_store.backend,
                        token_reference=reference,
                        connected_by_user_id=scope.user_id,
                    )
                except asyncpg.UniqueViolationError as error:
                    raise self._already_connected(error) from None

        return self._view(installation)

    async def disconnect(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
    ) -> SlackIntegrationView:
        """Forget this workspace's installation, and say what is left.

        Idempotent: disconnecting a workspace that was not connected is a
        no-op reported as success. Two admins pressing the same button is an
        ordinary race, and an error on the second press would describe the
        first press rather than anything the second admin did wrong.

        What this does NOT do is revoke the token at Slack. `auth.revoke` is
        an outbound call that can fail while the row has already gone, which
        would leave a live credential recorded nowhere -- so the deletion is
        what happens here, and revocation belongs with a retryable job that
        can be handed the token before it is discarded. Until that job exists,
        an admin who needs the grant gone at Slack removes the app there. This
        is stated rather than implied because the difference matters: after
        this returns, Vector cannot use the token and Slack still would.
        """
        self.require_admin(scope)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                # First, and in the same transaction. Every foreign key in
                # migration 018 is RESTRICT, so the delete below fails outright
                # while a channel row, a settings row or a preference row
                # survives -- which is the intended shape: a disconnect that
                # forgot one of them is a loud error rather than rows nothing
                # will read again. Inside one transaction, so a failure between
                # the two leaves the workspace connected with its channels
                # intact rather than half-disconnected.
                await self._repository.delete_dependents(
                    connection,
                    workspace_id=scope.workspace_id,
                )
                await self._repository.delete(
                    connection,
                    workspace_id=scope.workspace_id,
                )

        return self._view(None)

    async def bot_token(self, *, scope: AuthorizedWorkspaceScope) -> str | None:
        """This workspace's bot token, for code that has to present it.

        The one path in the codebase that yields a live credential, which is
        why it is a method a caller has to name rather than a field on an
        entity. Nothing in the GraphQL layer calls it, and nothing in it may:
        the schema has no field this could fill.

        A row written by a store this process does not have raises rather than
        being handed over -- a KMS reference passed to the database store
        would otherwise be returned as if it were the token itself, and sent
        to Slack as an Authorization header.
        """
        self.require_admin(scope)

        async with self._pool.acquire() as connection:
            stored = await self._repository.find_token_reference(
                connection,
                workspace_id=scope.workspace_id,
            )

        if stored is None:
            return None

        return await self._fetch_token(stored, workspace_id=scope.workspace_id)

    async def _fetch_token(
        self,
        stored: tuple[str, str],
        *,
        workspace_id: UUID,
    ) -> str:
        """This service's route to the module function below.

        Takes no scope and performs no authorization. Every caller has already
        done `require_admin`; a second check here would suggest this is a
        boundary, and it is not -- it is the private half of one.
        """
        return await fetch_token(
            self._token_store,
            stored,
            workspace_id=workspace_id,
        )

    # --- channels, settings and preferences -----------------------------

    async def channels(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
    ) -> tuple[SlackChannelEntity, ...]:
        """This workspace's cached channel list. No network call.

        A read of what the last sync left behind, deliberately: a settings page
        that called `conversations.list` on every render would spend a
        workspace's Slack rate limit on people looking at a page, and would
        fail to render at all whenever Slack is slow. Refreshing is
        `sync_channels`, which is a mutation because it changes what is stored.

        An unconfigured deployment answers empty without a query, for the
        reason `status` gives: with no Slack app there is no row that could
        mean anything.
        """
        self.require_admin(scope)

        if not self._configured:
            return ()

        async with self._pool.acquire() as connection:
            listed = await self._repository.list_channels(
                connection,
                workspace_id=scope.workspace_id,
            )

        return tuple(listed)

    async def sync_channels(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
    ) -> SlackChannelSync:
        """Ask Slack what channels exist, and record the answer.

        The order is the method. Everything the database can answer is read
        first, on one connection which is then RELEASED -- the network call
        happens with no connection held, because a pool exhausted by admins
        waiting on Slack is an outage in the rest of the product caused by a
        settings page.

        Three refusals happen before any call is made, and each is a different
        sentence to an admin: no installation, a grant that does not carry
        `channels:read` because the admin declined it, and -- via
        `_fetch_token` -- a token this process cannot read.

        The granted scope is checked against `slack_installations.scopes` and
        never against REQUESTED_SCOPES. What this release asks for says nothing
        about what a particular workspace permitted; migration 014 stores the
        granted list precisely so a feature gated on a scope can read what was
        agreed to.

        A failure answers with the CACHED channels rather than an empty list. A
        picker that blanked itself every time Slack was slow would look like a
        workspace that had lost its channels, and the cache is still the best
        available answer to "what can we post to".
        """
        self.require_admin(scope)

        if not self._configured:
            return SlackChannelSync(channels=(), failure="not_connected")

        async with self._pool.acquire() as connection:
            installation = await self._repository.find(
                connection,
                workspace_id=scope.workspace_id,
            )
            cached = tuple(
                await self._repository.list_channels(
                    connection,
                    workspace_id=scope.workspace_id,
                )
            )
            stored = await self._repository.find_token_reference(
                connection,
                workspace_id=scope.workspace_id,
            )

        if installation is None or stored is None:
            return SlackChannelSync(channels=cached, failure="not_connected")

        if SCOPE_LIST_CHANNELS not in installation.scopes:
            return SlackChannelSync(channels=cached, failure="missing_scope")

        token = await self._fetch_token(stored, workspace_id=scope.workspace_id)

        try:
            listed = await self._web_api().conversations_list(token=token)
        except SlackApiError as error:
            # The only exception caught here, and it is the one this seam
            # raises for every way a Slack call can fail. A broader `except
            # Exception` would report a bug in the mapping code as "Slack is
            # unreachable", which is a wrong answer an admin would act on.
            return SlackChannelSync(channels=cached, failure=_failure_for(error))

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.replace_channels(
                    connection,
                    workspace_id=scope.workspace_id,
                    channels=listed,
                )
                channels = tuple(
                    await self._repository.list_channels(
                        connection,
                        workspace_id=scope.workspace_id,
                    )
                )

        return SlackChannelSync(channels=channels, failure=None)

    async def notification_settings(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
    ) -> SlackNotificationSettingsEntity:
        """Where this workspace posts, and which events it has turned on."""
        self.require_admin(scope)

        if not self._configured:
            return self._settings(None, [])

        async with self._pool.acquire() as connection:
            chosen = await self._repository.find_default_channel(
                connection,
                workspace_id=scope.workspace_id,
            )
            stored = await self._repository.list_preferences(
                connection,
                workspace_id=scope.workspace_id,
            )

        return self._settings(chosen, stored)

    async def set_default_channel(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        channel_id: str,
    ) -> SlackNotificationSettingsEntity:
        """Choose where this workspace's Slack notifications go.

        `channel_id` is the one value on this path a client chose, and it is
        never trusted as a channel: it is looked up in `slack_channels` scoped
        to the caller's own workspace, and the NAME that gets stored comes from
        that row rather than from anything the client sent. A channel belonging
        to another tenant answers exactly as one that does not exist -- the
        composite foreign key in 018 would refuse the write anyway, but a
        refusal an admin sees as a form error beats one they see as a 500.

        Archived and inaccessible channels are refused here rather than at the
        database, because they are storable and simply do not work: posting to
        an archived channel fails, and the moment to say so is while an admin
        is choosing rather than the first time an issue is assigned.

        The read and the write are in one transaction. Between them a sync can
        mark the channel inaccessible, and outside a transaction the check
        would have passed against a row the write then contradicts.
        """
        self.require_admin(scope)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                self._require_connected(
                    await self._repository.find(
                        connection,
                        workspace_id=scope.workspace_id,
                    )
                )

                channel = await self._repository.find_channel(
                    connection,
                    workspace_id=scope.workspace_id,
                    channel_id=channel_id,
                )

                if channel is None or not channel.is_accessible:
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="channelId",
                                code="unknown_channel",
                                message=(
                                    "That channel is not one this workspace can "
                                    "post to. Refresh the channel list and choose "
                                    "again."
                                ),
                            )
                        ]
                    )

                if channel.is_archived:
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="channelId",
                                code="channel_archived",
                                message=(
                                    "That channel is archived, so messages sent to "
                                    "it would go nowhere."
                                ),
                            )
                        ]
                    )

                await self._repository.set_default_channel(
                    connection,
                    workspace_id=scope.workspace_id,
                    channel_id=channel.channel_id,
                    channel_name=channel.name,
                )

                stored = await self._repository.list_preferences(
                    connection,
                    workspace_id=scope.workspace_id,
                )

        return self._settings((channel.channel_id, channel.name), stored)

    async def set_notification_preference(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        event: str,
        enabled: bool,
    ) -> SlackNotificationSettingsEntity:
        """Turn one event on or off for this workspace.

        The event is checked against SLACK_NOTIFICATION_EVENTS here even though
        the GraphQL enum already makes an unknown one unrepresentable, and even
        though `slack_notification_preferences_event_known` would refuse it at
        the database. The enum guards one transport and the CHECK produces a
        constraint violation -- a 500 to a client -- so the rule lives in the
        layer that owns rules and answers as a field error.
        """
        self.require_admin(scope)

        if event not in SLACK_NOTIFICATION_EVENTS:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="event",
                        code="unknown_event",
                        message="That is not an event Vector announces in Slack.",
                    )
                ]
            )

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                self._require_connected(
                    await self._repository.find(
                        connection,
                        workspace_id=scope.workspace_id,
                    )
                )

                await self._repository.set_preference(
                    connection,
                    workspace_id=scope.workspace_id,
                    event=event,
                    enabled=enabled,
                )

                chosen = await self._repository.find_default_channel(
                    connection,
                    workspace_id=scope.workspace_id,
                )
                stored = await self._repository.list_preferences(
                    connection,
                    workspace_id=scope.workspace_id,
                )

        return self._settings(chosen, stored)

    async def send_test_notification(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
    ) -> SlackDeliveryResult:
        """Post the test message, and report what actually happened.

        The whole point of this method is that it cannot lie. `delivered=True`
        is reachable from exactly one place -- after `post_message` returned
        without raising, which happens only for a Slack response that said
        `ok: true`. There is no branch that catches an exception, logs it and
        reports success, and no default that starts out True.

        Every refusal before the call is one an admin can act on: not
        connected, a grant missing `chat:write`, or no channel chosen. Every
        refusal after it comes from `_failure_for`, which is the one frame that
        reads Slack's error strings.

        The commonest real failure is `not_in_channel`: Vector holds
        `chat:write`, which permits posting to channels the bot has JOINED, and
        a public channel it has not been invited to refuses. That arrives as
        CHANNEL_UNAVAILABLE, and it is why `SlackChannel.isMember` exists --
        so the product can say "invite Vector to #engineering" rather than
        leaving an admin to guess. The alternative is `chat:write.public`,
        which is a wider grant than this integration has been given.

        The connection is released before the call, for the reason
        `sync_channels` gives.
        """
        self.require_admin(scope)

        if not self._configured:
            return SlackDeliveryResult(delivered=False, failure="not_connected")

        async with self._pool.acquire() as connection:
            installation = await self._repository.find(
                connection,
                workspace_id=scope.workspace_id,
            )
            stored = await self._repository.find_token_reference(
                connection,
                workspace_id=scope.workspace_id,
            )
            chosen = await self._repository.find_default_channel(
                connection,
                workspace_id=scope.workspace_id,
            )

        if installation is None or stored is None:
            return SlackDeliveryResult(delivered=False, failure="not_connected")

        if SCOPE_POST_MESSAGE not in installation.scopes:
            return SlackDeliveryResult(delivered=False, failure="missing_scope")

        if chosen is None:
            return SlackDeliveryResult(delivered=False, failure="no_default_channel")

        token = await self._fetch_token(stored, workspace_id=scope.workspace_id)
        channel_id, _ = chosen

        try:
            await self._web_api().post_message(
                token=token,
                channel=channel_id,
                text=TEST_MESSAGE_TEXT,
            )
        except SlackApiError as error:
            return SlackDeliveryResult(delivered=False, failure=_failure_for(error))

        return SlackDeliveryResult(delivered=True, failure=None)

    def _web_api(self) -> SlackWebApi:
        """The Web API seam, or a loud failure.

        A service built without one can still do everything that does not talk
        to Slack over the network -- the OAuth callback builds exactly such an
        instance -- so the absence is checked here, at use, rather than in
        `__init__`. RuntimeError rather than a failure value: this is a
        composition mistake, not a state of the world, and reporting it as
        SLACK_UNREACHABLE would send an operator to check Slack's status page
        for a missing constructor argument.
        """
        if self._web is None:
            raise RuntimeError(
                "this SlackService was built without a Web API client; "
                "the composition root must pass `web=`"
            )

        return self._web

    @staticmethod
    def _require_connected(installation: SlackInstallationEntity | None) -> None:
        """Refuse a write that would land on a workspace with no installation.

        Every table in migration 018 hangs off `slack_installations`, so
        without this the write fails on a foreign key -- an integrity error,
        which reaches a client as a masked internal error and reaches the log
        as a traceback for something that is not a defect. A field error says
        the actionable thing instead: connect Slack first.
        """
        if installation is None:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="workspaceSlug",
                        code="not_connected",
                        message="This workspace has not connected Slack.",
                    )
                ]
            )

    @staticmethod
    def _settings(
        chosen: tuple[str, str] | None,
        stored: list[SlackNotificationPreference],
    ) -> SlackNotificationSettingsEntity:
        """The client-facing shape, with every event present exactly once.

        The merge is here rather than in the repository because "what does an
        unwritten preference mean" is a product decision, and this is the layer
        that holds those. Today it means off: posting into a company's Slack is
        a visible act, so it is opt-in per event rather than something that
        starts happening the moment a channel is picked.

        Every event in the vocabulary appears, in the vocabulary's order,
        whether or not a row exists. A client rendering a settings screen from
        the stored rows alone would be missing a toggle for every event nobody
        had touched -- which is all six on the day Slack is connected.

        A stored row for an event this release does not know is dropped rather
        than surfaced. That is what makes retiring an event a code change: the
        row can stay in the table until someone deletes it, and no client is
        handed a value its enum cannot name.
        """
        by_event = {preference.event: preference.enabled for preference in stored}

        preferences = tuple(
            SlackNotificationPreference(
                event=event,
                enabled=by_event.get(event, False),
            )
            for event in SLACK_NOTIFICATION_EVENTS
        )

        return SlackNotificationSettingsEntity(
            default_channel_id=chosen[0] if chosen is not None else None,
            default_channel_name=chosen[1] if chosen is not None else None,
            preferences=preferences,
        )

    async def installation_for_team(
        self,
        *,
        slack_team_id: str,
    ) -> SlackInstallationEntity | None:
        """The installation an inbound event belongs to, or nothing.

        Takes no scope, because on this path there is no viewer: the caller is
        Slack, and the evidence that the request is genuine is the verified
        request signature the transport checked before calling. This method
        must therefore never be reachable from a request that was not
        signature-verified, which is why its only caller is the events
        endpoint and the verification is the first thing that endpoint does.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.find_by_team(
                connection,
                slack_team_id=slack_team_id,
            )

    async def claim_event(self, *, event_id: str) -> bool:
        """Claim one Slack event id. True if this delivery is the first.

        Its own transaction, committed before the caller does any work with
        the event, and that ordering is the point. A claim held open until the
        processing finished would be invisible to the retry that Slack sends
        three seconds later -- an uncommitted row blocks a concurrent insert
        rather than being seen by it, so the retry would wait on a lock and
        then find the row and stop, which is correct but only by accident of
        timing. Committing first makes "already claimed" a fact the retry
        reads immediately.

        The trade is stated rather than hidden: if the process dies between
        the claim and the work, the event is claimed and never processed, and
        Slack's retry will find it claimed. That is at-most-once for a crash
        window, chosen over the at-least-once alternative because the failure
        this endpoint actually sees is redelivery, many times a day, and not a
        crash mid-handler. The fix when it matters is an outbox row written in
        the same transaction as the claim, not a wider lock.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await self._repository.record_event(
                    connection,
                    event_id=event_id,
                )

    @staticmethod
    def require_admin(scope: AuthorizedWorkspaceScope) -> None:
        """Refuse anyone but an admin or owner, indistinguishably from a miss.

        Public, because the OAuth start route needs this exact refusal before
        it issues a state and has no service call of its own to hang it on.
        Exposing it is what keeps the rule in one place: the alternative is
        the transport comparing roles itself, which is a second copy of an
        authorization decision and the one that drifts.

        WorkspaceAccessDeniedError, the same exception a non-member gets, and
        the transports render it as NOT_FOUND. That is deliberate: to a member
        who may not administer the workspace, the integration does not exist.
        A distinct "forbidden" would tell any member that this deployment has
        a Slack app, that this workspace is or is not connected to it, and
        that the road to it is being an admin -- which is three facts more
        than the refusal needs to convey, on a surface reachable by everyone
        in the tenant.

        Reading the role off the scope, never off an argument: the role came
        from the `workspace_members` row that MembershipService matched, and
        AuthorizedWorkspaceScope is frozen so nothing between there and here
        could have raised it.
        """
        if scope.role not in SLACK_ADMIN_ROLES:
            raise WorkspaceAccessDeniedError()

    @staticmethod
    def _already_connected(error: asyncpg.UniqueViolationError) -> Exception:
        """Translate a Slack workspace that is taken, or hand back the original.

        Returned rather than raised so the call site reads `raise ... from
        None`, matching CycleService: this is the one place a server error
        becomes an expected outcome, and it should be visible as a raise where
        it happens.

        Narrowed to the one constraint. A unique violation from a constraint
        added by a later migration is not this error and must not be described
        to an admin as one.
        """
        if error.constraint_name != SLACK_INSTALLATIONS_TEAM_KEY:
            # Annotated rather than returned inline: asyncpg ships no types,
            # so the parameter is Any and would silently satisfy any return
            # type this function grew later.
            unexpected: Exception = error

            return unexpected

        return SlackTeamAlreadyConnectedError()

    def _view(
        self,
        installation: SlackInstallationEntity | None,
    ) -> SlackIntegrationView:
        """The client-facing shape, built in one place.

        One function so that the three answers cannot drift into carrying
        different fields -- in particular so that "disconnected" is always
        empty scopes and no team name, rather than whatever the last
        installation happened to leave behind.

        The configuration check is here rather than only in `status` so that
        every method answers consistently. Without it, `disconnect` on a
        deployment with no Slack app would report DISCONNECTED -- which is the
        state whose UI is a connect button, offered immediately after an admin
        pressed disconnect, on a deployment where connecting cannot work.
        """
        if not self._configured:
            return SlackIntegrationView(
                status="unconfigured",
                team_name=None,
                scopes=(),
            )

        if installation is None:
            return SlackIntegrationView(
                status="disconnected",
                team_name=None,
                scopes=(),
            )

        return SlackIntegrationView(
            status="connected",
            team_name=installation.slack_team_name,
            scopes=installation.scopes,
        )
