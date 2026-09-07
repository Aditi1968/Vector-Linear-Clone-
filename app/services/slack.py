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

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.slack import (
    SLACK_ADMIN_ROLES,
    SlackGrant,
    SlackInstallationEntity,
    SlackIntegrationView,
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
    ):
        self._pool = pool
        self._repository = repository
        self._token_store = token_store
        self._configured = configured

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

        backend, reference = stored

        if backend != self._token_store.backend:
            raise RuntimeError(
                f"stored Slack token uses the {backend!r} store; "
                f"this process is configured with {self._token_store.backend!r}"
            )

        return await self._token_store.fetch(
            workspace_id=scope.workspace_id,
            reference=reference,
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
