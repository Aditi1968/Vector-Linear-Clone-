"""The GitHub App integration: what is configured, what is connected, and
what a signed delivery is allowed to change.

Three things live here that are not a service method, and each is here rather
than in `app/rest/github.py` because it is a rule rather than a transport
detail: what counts as a configured deployment, how GitHub proves a payload
came from GitHub, and where a browser may be sent afterwards. The REST layer
reads the raw body and the query string; every decision it makes, it makes by
calling one of these.
"""

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
import httpx
from pydantic import SecretStr

from app.config import Settings
from app.domain.errors import (
    GithubNotConfiguredError,
    WorkspaceAccessDeniedError,
)
from app.domain.github import (
    CONNECTED,
    DISCONNECTED,
    PENDING,
    UNCONFIGURED,
    GithubInstallationEntity,
    GithubIntegrationEntity,
    GithubRepositoryEntity,
)
from app.domain.tenancy import AuthorizedWorkspaceScope, WorkspaceScope
from app.repositories.github import GithubRepository


# Who may see or change a workspace's GitHub integration.
#
# Connecting a GitHub App grants a third party read access to the
# organisation's source code, and disconnecting it silently breaks whatever
# was built on top. Neither is an ordinary member's action, so both are
# refused for the `member` role -- and refused with the same answer a
# non-member gets, so that the refusal never doubles as confirmation that an
# integration exists. See `require_workspace_admin`.
GITHUB_ADMIN_ROLES: Final = ("admin", "owner")

# What GitHub prefixes the digest with in X-Hub-Signature-256. Part of the
# signed comparison rather than stripped off first: comparing only the hex
# half would accept a header that named some other algorithm entirely.
SIGNATURE_PREFIX: Final = "sha256="

# The header itself. Named here because two modules have to agree on it and a
# typo would read as "no signature", which fails closed but fails for every
# delivery at once.
SIGNATURE_HEADER: Final = "X-Hub-Signature-256"

# How long an unconfirmed claim stays open.
#
# A claim is a workspace saying "I just installed the app, and it is
# installation N". Nothing proves that, so the claim only becomes a connection
# if GitHub names N in a signed delivery before this runs out -- and an
# attacker naming somebody else's installation cannot make GitHub emit
# anything, so their claim simply expires holding nothing.
#
# Fifteen minutes rather than five: `installation.created` goes through
# GitHub's delivery queue while the browser redirect that records the claim
# takes one hop, so the two arrive in either order and a delivery that has been
# retried needs room. Rather than fifteen hours, because every minute is a
# minute in which a guessed id could be confirmed by the real owner's install.
#
# Not a column. The deadline is a policy, so it is compared against
# `connected_at` at read time and shortening it takes effect on claims already
# in flight -- see migrations/016_github_installation_trust.sql.
CLAIM_TTL: Final = timedelta(minutes=15)

# The `installation` actions that accompany a live installation, and therefore
# the only ones that may confirm a claim.
#
# `created` is the ordinary one. The other two are here because `created` is
# dispatched exactly once and can lose the race with the browser redirect that
# records the claim -- a workspace whose claim was written a second too late
# would otherwise have no way to a connection but uninstalling and starting
# again.
#
# `deleted` and `suspend` are deliberately absent: they name an installation
# that is ending or already stopped, and confirming a claim from one would
# hand the claimant a connection to an organisation that has just revoked the
# app. Every `installation_repositories` action is absent for the same reason
# and a stronger one -- those payloads carry private repository names, which
# is exactly what an unconfirmed claim must never be able to collect.
#
# ponytail: a delivery that ARRIVES BEFORE the claim is dropped, so an
# `installation.created` that beats the browser redirect leaves the workspace
# PENDING until the window closes. It is recoverable rather than terminal --
# removing the app on GitHub and installing it again dispatches a fresh
# `created`, which the next claim is in time for -- and the two actions above
# catch some of the rest. The upgrade, if that recovery turns out to be one
# too many steps, is to record the unclaimed delivery (installation id,
# account, repository list, witnessed_at) in a table no workspace can read and
# let a claim landing inside the same window confirm against it. Not built
# now: it is a second copy of another organisation's data at rest, for a race
# that costs an admin one reinstall.
CONFIRMING_ACTIONS: Final = frozenset(
    {"created", "new_permissions_accepted", "unsuspend"}
)


def _private_key(settings: Settings) -> str | None:
    """The app's PEM, from wherever this deployment keeps it.

    Two sources because deployments differ and neither is wrong. A container
    platform injects the key as an environment variable; a developer has the
    `.pem` GitHub handed them and a path to it, because a PEM is multi-line
    and a multi-line `.env` value is a quoting problem with a different answer
    in every tool.

    The inline value wins when both are set, and that ordering is deliberate
    rather than arbitrary: an explicitly injected secret is the more specific
    statement, and a stale path left in a `.env` should not override it.

    A path that does not exist, or cannot be read, returns None rather than
    raising. That makes the deployment UNCONFIGURED -- the state the whole
    integration is already built to handle honestly -- instead of a crash at
    the composition root that takes down an application whose GitHub
    integration nobody may be using. The error is not swallowed silently: it
    is what `configured` then reports, and the settings screen says so.

    Nothing here logs the path's CONTENTS, and the OSError is not chained
    into anything that reaches a client.
    """
    inline = _secret(settings.github_app_private_key)

    if inline is not None:
        return inline

    path = settings.github_private_key_path

    if not path:
        return None

    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


@dataclass(frozen=True, slots=True)
class GithubAppConfig:
    """The deployment's GitHub App credentials, or the absence of them.

    Built from Settings at the composition root and passed down, so that no
    layer below reads the environment and no two layers can disagree about
    whether this deployment has a GitHub App.

    `__repr__` is overridden and that override is load-bearing. A dataclass
    prints every field, so the default one would put an RSA private key into
    any traceback, log line or debugger that touched an object holding this --
    and objects holding this are the service and the REST handlers, which is
    to say the frames an exception unwinds through. The replacement prints
    exactly one fact, which is the only one anybody debugging needs.
    """

    app_id: str | None
    private_key: str | None
    webhook_secret: str | None
    client_id: str | None
    client_secret: str | None

    # Origins only -- scheme and host, no path -- already normalised. See
    # `allowed_redirect`.
    redirect_allowlist: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> "GithubAppConfig":
        """Unwrap the secrets exactly once, at the edge of the process.

        `get_secret_value()` is deliberate rather than incidental: after this
        point the values are plain strings, so this is the frame to look at
        when asking where a key can travel from. It travels into this object
        and nowhere else -- nothing below hands one to a template, a response
        or a log.
        """
        return cls(
            app_id=settings.github_app_id,
            private_key=_private_key(settings),
            webhook_secret=_secret(settings.github_webhook_secret),
            client_id=settings.github_client_id,
            client_secret=_secret(settings.github_client_secret),
            redirect_allowlist=_origins(settings.github_redirect_allowlist),
        )

    @property
    def configured(self) -> bool:
        """Whether this deployment has a usable GitHub App.

        All five or none, deliberately. A deployment with a client id and no
        webhook secret can start an install flow it cannot finish -- the
        callback lands, the row is written, and every delivery that would have
        filled in the account and the repositories is rejected unverified. A
        partial configuration is a misconfiguration, and reporting it as
        "configured" would put a Connect button in front of a user whose click
        produces a half-connected workspace.

        The redirect allowlist is deliberately NOT part of this. It decides
        where a browser goes after the flow, not whether the flow can run: with
        none set the callback answers with a plain page instead of a redirect,
        which is a worse experience and a working integration.
        """
        return all(
            (
                self.app_id,
                self.private_key,
                self.webhook_secret,
                self.client_id,
                self.client_secret,
            )
        )

    def __repr__(self) -> str:
        return f"GithubAppConfig(configured={self.configured})"


def _secret(value: SecretStr | None) -> str | None:
    """The plaintext behind a SecretStr, or None if it was never set."""
    return None if value is None else value.get_secret_value()


def _origins(allowlist: str) -> tuple[str, ...]:
    """Parse the comma-separated allowlist into normalised origins.

    Anything that is not an absolute http(s) URL is dropped rather than
    reported. The alternative -- refusing to start -- would take a deployment
    down over the redirect target of an integration it may not even use, and
    the failure mode of dropping is safe: an entry that does not survive
    parsing is an origin the callback will not redirect to.
    """
    parsed = (_origin(entry.strip()) for entry in allowlist.split(","))

    return tuple(origin for origin in parsed if origin is not None)


def _origin(url: str) -> str | None:
    """`scheme://host[:port]`, lowercased, or None if this is not a URL.

    The origin and nothing else is what gets compared, which is what makes
    the check hold against the shapes that beat a `startswith`:
    `https://good.example.evil.test` has a different host,
    `https://good.example@evil.test` puts the allowed name in the userinfo and
    the attacker's in the host (so it lands in `netloc` here and does not
    match), and `//evil.test/x` has no scheme at all and is rejected outright
    rather than inheriting the current page's.
    """
    parts = urlsplit(url)

    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None

    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def allowed_redirect(
    candidate: str | None,
    *,
    allowlist: Sequence[str],
) -> str | None:
    """Where to send the browser after the install callback.

    Never the candidate unchecked. `candidate` reaches here from a query
    parameter -- by way of a cookie this server wrote, which is not the same
    as a value this server chose -- and an open redirect on an OAuth callback
    is how a phishing page borrows a real domain's name in the address bar.

    Returns:
      * the candidate, when its origin is on the allowlist;
      * the first allowed origin, when it is not (a rejected target is a bug
        or an attack, and either way the user still belongs back in the app);
      * None, when the deployment configured no allowlist at all, which the
        caller answers with a plain page rather than a redirect to a guess.
    """
    if not allowlist:
        return None

    if candidate is not None and _origin(candidate) in set(allowlist):
        return candidate

    return allowlist[0]


def verify_webhook_signature(
    *,
    secret: str | None,
    body: bytes,
    header: str | None,
) -> bool:
    """Whether `header` is GitHub's HMAC-SHA256 over exactly these bytes.

    Over the RAW body, which is why the caller must not have parsed it yet:
    `json.loads` followed by `json.dumps` is not the same bytes -- key order,
    separators and unicode escaping all differ -- so a signature verified
    against a re-serialised payload verifies nothing about what arrived.

    `hmac.compare_digest` rather than `==`. String equality returns as soon as
    two bytes differ, and the time it took to return is a measurement of how
    many leading bytes were right; a few thousand requests turn that into the
    signature, one byte at a time, without ever knowing the secret.

    False for a missing header, an unconfigured secret and a wrong digest
    alike. The caller must not tell them apart in a response: which of the
    three it was is the difference between "this deployment has no webhook
    secret" and "your guess was wrong", and neither is anyone's business.

    The ASCII guard is not decoration. `compare_digest` raises TypeError on
    non-ASCII `str` arguments, so a header of one multi-byte character would
    be a 500 -- an unauthenticated crash on the one endpoint that is
    deliberately reachable by anyone.
    """
    if not secret or header is None or not header.isascii():
        return False

    expected = (
        SIGNATURE_PREFIX
        + hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
    )

    return hmac.compare_digest(expected, header)


def require_workspace_admin(scope: AuthorizedWorkspaceScope) -> None:
    """Refuse anyone below admin, with the answer a stranger gets.

    WorkspaceAccessDeniedError, not a distinct "forbidden": a member who can
    tell "you may not see this" from "there is nothing here" learns whether
    their workspace has connected a GitHub organisation, which is the business
    of the admins who connected it. Both transports turn this into the same
    NOT_FOUND a non-member gets.

    A function rather than a method, because the install redirect has to make
    the same decision before the service is ever called -- sending an ordinary
    member off to GitHub to install an app they will not be allowed to record
    is a worse refusal than refusing at the start.
    """
    if scope.role not in GITHUB_ADMIN_ROLES:
        raise WorkspaceAccessDeniedError()


def _positive_int(value: Any) -> int | None:
    """A positive integer from a JSON value, or None.

    `bool` is excluded explicitly because it is a subclass of `int` in Python,
    so `True` would otherwise arrive as the installation id 1 -- which is a
    real row in somebody's database.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None

    # Annotated rather than returned inline: `value` is Any, and returning it
    # directly would satisfy any return type this function declared.
    number: int = value

    return number


def _repositories(value: Any) -> tuple[GithubRepositoryEntity, ...] | None:
    """Parse a webhook's repository list, or None if there was not one.

    None and `()` mean different things and both occur: None is "this payload
    said nothing about repositories" (leave the stored set alone), and an empty
    tuple is "this payload listed none" (the set is now empty). Collapsing them
    would make an `installation` event that omits the list wipe the workspace's
    repositories.

    Malformed entries are skipped rather than raising. The payload is already
    proven to be GitHub's by the time this runs, so a bad entry is a schema
    change rather than an attack -- and raising would fail the whole delivery,
    which GitHub then retries, which fails again.

    Duplicates are collapsed by id, because the writer has no ON CONFLICT and
    a repeated id would be a primary key violation.
    """
    if not isinstance(value, list):
        return None

    found: dict[int, GithubRepositoryEntity] = {}

    for entry in value:
        if not isinstance(entry, Mapping):
            continue

        repository_id = _positive_int(entry.get("id"))
        full_name = entry.get("full_name")

        if repository_id is None or not isinstance(full_name, str):
            continue

        found[repository_id] = GithubRepositoryEntity(
            repository_id=repository_id,
            full_name=full_name,
        )

    return tuple(found.values())


# --- proving the installer owns what they claimed -----------------------

# GitHub's OAuth token endpoint, and the API root the resulting user token is
# spent against. Module constants and never arguments, so no caller can aim
# this exchange at a host of their choosing.
GITHUB_TOKEN_URL: Final = "https://github.com/login/oauth/access_token"
GITHUB_USER_INSTALLATIONS_URL: Final = "https://api.github.com/user/installations"

# How long the two calls below may take. Short, because they sit inside an
# OAuth callback the user is watching, and a provider that has stopped
# answering must not hold the request open.
VERIFY_TIMEOUT_SECONDS: Final = 10.0


class InstallationOwnershipCheck(Protocol):
    """Whether the person finishing this flow can reach this installation."""

    async def installed_for_user(self, *, code: str, installation_id: int) -> bool: ...


class GithubUserInstallations:
    """Asks GitHub, using the grant the installer just consented to.

    This is the check `connect` could not make. An installation id arrives in
    a query string and GitHub's ids are a small ascending counter, so naming
    another organisation's is a guess anyone can make -- and the claim design
    exists precisely because nothing in the redirect proves otherwise.

    A signed `installation` delivery proves it, but only for an app being
    installed for the first time: an app already installed emits no
    `installation.created`, so a workspace connecting to an existing
    installation would wait for a delivery that never comes.

    The OAuth code closes that gap and is the only thing in the redirect that
    can. It is single-use, issued by GitHub to this app for this browser, and
    exchanges for a token whose `GET /user/installations` lists exactly the
    installations that account may administer. An id in that list is one the
    person clicking genuinely has; an id they guessed is not.

    The user token is spent immediately and never stored, logged or returned.
    It is a bearer credential for someone's whole GitHub account, and the only
    safe thing to do with one is use it once and let it fall out of scope --
    which is why this returns a bool rather than anything derived from it.
    """

    def __init__(self, *, client_id: str, client_secret: str, redirect_uri: str | None):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    async def installed_for_user(self, *, code: str, installation_id: int) -> bool:
        fields = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
        }

        # Deliberately absent, matching the authorize leg. GitHub compares
        # the two and refuses the exchange when one sends a redirect_uri and
        # the other does not, so this is not an omission but the other half of
        # the same decision -- see the note in app/rest/github.py's install
        # handler for why neither sends it.

        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SECONDS) as client:
            granted = await client.post(
                GITHUB_TOKEN_URL, data=fields, headers={"Accept": "application/json"}
            )

            if granted.status_code != 200:
                return False

            token = granted.json().get("access_token")

            if not isinstance(token, str) or not token:
                return False

            # One page is enough for the question being asked; an account with
            # more than a hundred installations is not a case this flow has,
            # and asking for more would turn a verification into a crawl.
            listed = await client.get(
                GITHUB_USER_INSTALLATIONS_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 100},
            )

        if listed.status_code != 200:
            return False

        return any(
            entry.get("id") == installation_id
            for entry in (listed.json().get("installations") or [])
        )


class GithubService:
    """Business rules for a workspace's GitHub App installation.

    The service owns connection acquisition and transaction boundaries, and it
    owns two rules that exist nowhere else: only an admin or owner may see or
    change an integration, and a deployment with no GitHub App cannot connect
    one however well-formed the request is.

    Nothing this class returns carries a credential. The entities it hands
    back are built from columns that do not exist for a token, a key or a
    signature, and the config object it holds is never returned at all -- only
    `configured`, which is a boolean.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: GithubRepository,
        config: GithubAppConfig,
        ownership: "InstallationOwnershipCheck | None" = None,
    ):
        # Optional, and None is a real deployment rather than a degraded one:
        # without it a claim is confirmed only by a signed `installation`
        # delivery, which is exactly the behaviour 016 shipped. Supplying one
        # adds a second, independent way to establish the same fact, and
        # neither weakens the other -- both end at the same single writer of
        # `confirmed_at`.
        self._ownership = ownership
        self._pool = pool
        self._repository = repository
        self._config = config

    @property
    def config(self) -> GithubAppConfig:
        """The app credentials, for the transport that has to build a URL.

        Exposed because the install redirect needs the client id and the
        webhook receiver needs the secret to verify against, and both of those
        are transports. It is not exposed to GraphQL: no resolver reads this,
        and `GithubAppConfig` has no Strawberry type, so there is no field a
        client could select it through.
        """
        return self._config

    async def integration_for(
        self,
        scope: AuthorizedWorkspaceScope,
    ) -> GithubIntegrationEntity:
        """This workspace's integration, as an admin of it sees it.

        A single read needs no write transaction, so this acquires a
        connection without opening one. The repositories are only read when
        there is an installation to read them for -- an unconnected workspace
        has none by construction, and asking would be a second round trip to
        establish it.
        """
        require_workspace_admin(scope)

        async with self._pool.acquire() as connection:
            installation = await self._repository.get_installation(
                connection,
                scope=scope,
            )

            repositories: Sequence[GithubRepositoryEntity] = ()

            if installation is not None:
                repositories = await self._repository.list_repositories(
                    connection,
                    scope=scope,
                )

        return self._view(installation, repositories)

    async def confirm_with_user_grant(
        self,
        *,
        installation_id: int,
        code: str,
    ) -> bool:
        """Promote a claim when GitHub says this installer owns it.

        The second route to `confirmed_at`, and the one that makes connecting
        to an ALREADY-INSTALLED app possible at all: GitHub emits
        `installation.created` once, so a workspace joining an existing
        installation would otherwise wait for a delivery that never arrives
        and sit at PENDING until the claim expired.

        The evidence is different from the webhook's but no weaker. A signed
        delivery is GitHub telling us an installation happened; the OAuth code
        is GitHub telling us that THIS browser's account can administer THIS
        installation. The second is a closer answer to the question the claim
        actually poses -- who clicked -- and the id is compared against a list
        GitHub returns rather than anything the client supplied.

        Fails closed everywhere: no checker configured, a refused exchange, a
        token that does not list the id, or any transport failure all leave
        the claim exactly as it was, for the webhook to confirm or the TTL to
        expire. It never writes `confirmed_at` on its own authority --
        `confirm_installation` is still the only writer, with the same three
        predicates, so a claim that is already confirmed, already expired or
        belongs to nobody is untouched.
        """
        if self._ownership is None:
            return False

        try:
            owned = await self._ownership.installed_for_user(
                code=code, installation_id=installation_id
            )
        except Exception:
            # A provider that is slow, down or answering nonsense must not
            # turn into a 500 in the middle of an OAuth callback. The claim
            # survives; PENDING is an honest state and the webhook path and
            # the TTL both still apply.
            return False

        if not owned:
            return False

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                workspace_id = await self._repository.confirm_installation(
                    connection,
                    installation_id=installation_id,
                    within=CLAIM_TTL,
                )

        return workspace_id is not None

    async def connect(
        self,
        scope: AuthorizedWorkspaceScope,
        *,
        installation_id: int,
    ) -> GithubIntegrationEntity:
        """Record this workspace's CLAIM on an installation. Answers PENDING.

        `installation_id` reaches here from a query string, and this method is
        written on the assumption that it may be a lie. It records who claimed
        what and stops; the workspace is not connected to anything until a
        delivery GitHub signed names the same installation while the claim is
        still open -- see `apply_webhook` and CLAIM_TTL. Before
        migrations/016_github_installation_trust.sql this method reported
        CONNECTED here, which is how an admin of one workspace could take
        delivery of another organisation's account name and repositories.

        `connected_by` comes from the scope and never from an argument, for
        the reason MembershipService gives about `user_id`: an argument is
        something a caller can choose, and this column is the answer to "who
        gave this app access to our code".

        Re-connecting replaces rather than merges. A workspace that installs
        the app into a different account must not keep the previous account's
        repositories, so the whole set goes first -- inside the same
        transaction, so there is no instant at which the workspace is
        connected to an account and holding another one's repository names.

        The expired-claim sweep is third, between this workspace's own rows
        going and the new claim landing, and it is what stops an abandoned
        claim locking an installation id away for good. It can only remove a
        row that is unconfirmed and out of time, so a *live* claim by another
        workspace survives it and the insert then raises
        GithubInstallationClaimedError -- the clean refusal, left to propagate
        because another workspace holding this installation is not something
        this layer can resolve.

        Raises GithubNotConfiguredError before touching the database. A row
        written by a deployment that cannot verify a webhook is a row nothing
        will ever confirm or clean up.
        """
        require_workspace_admin(scope)

        if not self._config.configured:
            raise GithubNotConfiguredError()

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.delete_repositories(connection, scope=scope)
                await self._repository.delete_installation(connection, scope=scope)
                await self._repository.delete_expired_claim(
                    connection,
                    installation_id=installation_id,
                    older_than=CLAIM_TTL,
                )

                installation = await self._repository.insert_installation(
                    connection,
                    scope=scope,
                    installation_id=installation_id,
                    connected_by=scope.user_id,
                )

        return self._view(installation, ())

    async def disconnect(
        self,
        scope: AuthorizedWorkspaceScope,
    ) -> GithubIntegrationEntity:
        """Forget this workspace's installation. Idempotent.

        Removes only Vector's record. The app stays installed on GitHub until
        somebody removes it there, which is deliberate: this server has no way
        to uninstall it without an API call, and pretending otherwise would
        leave an organisation believing access was revoked when it was not.
        What it does guarantee is that deliveries for that installation now
        resolve to no workspace and are dropped.

        Repositories first, because `github_repositories_installation_fk` is
        RESTRICT -- and in one transaction, so a failure between the two
        cannot leave repositories belonging to an installation that is gone.
        """
        require_workspace_admin(scope)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.delete_repositories(connection, scope=scope)
                await self._repository.delete_installation(connection, scope=scope)

        return self._view(None, ())

    async def apply_webhook(self, *, event: str, payload: Mapping[str, Any]) -> None:
        """Apply one verified delivery. Returns nothing, whatever happened.

        The caller has already verified GitHub's signature over the raw body;
        this method must never be reached by an unverified payload, which is
        why it takes the parsed mapping rather than the bytes -- parsing is
        what happens *after* verification, and a method that took bytes could
        be called in the wrong order.

        Silence is the contract. A delivery naming an installation no
        workspace has connected is ordinary -- the app is installed, the admin
        never finished the callback -- and so is an event this server has no
        rule for. Reporting either back to GitHub as a failure would earn a
        redelivery loop for something that will never succeed.

        The two events handled are the ones that change what the settings page
        shows: `installation` (the account, the initial repository set, and
        removal) and `installation_repositories` (the set changing later).
        Everything else is accepted and ignored.

        This method is also where a claim becomes a connection, and that is
        deliberate rather than convenient: the signature checked upstream is
        the only evidence this deployment ever receives about who owns an
        installation, so the confirmation has to happen where the signature
        has just been verified and nowhere else. See `_resolve_workspace`.

        `_apply_account` runs before any repository write, in one transaction,
        and that ordering is load-bearing beyond tidiness:
        `github_installations_unconfirmed_holds_no_account` in 016 aborts the
        whole delivery if this ever reached an unconfirmed row, and it does so
        before a private `full_name` from somebody else's organisation has
        been inserted.
        """
        if event not in ("installation", "installation_repositories"):
            return

        installation = payload.get("installation")

        if not isinstance(installation, Mapping):
            return

        installation_id = _positive_int(installation.get("id"))

        if installation_id is None:
            return

        async with self._pool.acquire() as connection:
            # One transaction for the whole delivery: an event that removes
            # some repositories and adds others must not be observable
            # half-applied, and the workspace lookup -- or the confirmation
            # that produced it -- has to hold for the writes that follow it.
            async with connection.transaction():
                workspace_id = await self._resolve_workspace(
                    connection,
                    event=event,
                    action=payload.get("action"),
                    installation_id=installation_id,
                )

                if workspace_id is None:
                    return

                scope = WorkspaceScope(workspace_id=workspace_id)

                if event == "installation" and payload.get("action") == "deleted":
                    # The app was uninstalled on GitHub. The integration is
                    # over whether or not anyone told Vector, so the row goes
                    # -- otherwise the settings page reports CONNECTED for an
                    # installation that no longer exists.
                    await self._repository.delete_repositories(
                        connection,
                        scope=scope,
                    )
                    await self._repository.delete_installation(
                        connection,
                        scope=scope,
                    )

                    return

                await self._apply_account(connection, scope, installation)

                if event == "installation":
                    await self._apply_full_set(connection, scope, payload)
                else:
                    await self._apply_delta(connection, scope, payload)

    async def _resolve_workspace(
        self,
        connection: asyncpg.Connection,
        *,
        event: str,
        action: Any,
        installation_id: int,
    ) -> UUID | None:
        """Whose installation this delivery is about, if it is anybody's.

        Two questions in order, and the order is the security property.

        First: is there a CONFIRMED installation with this id? That is the
        steady state and it never involves a claim, so a workspace that GitHub
        has already vouched for keeps receiving its deliveries whatever the
        action is.

        Only if there is not does a claim come into it, and then only for the
        actions that accompany a live installation. A claim is promoted at most
        once, by a delivery this server has verified GitHub sent, and only
        while the claim is young -- the repository's UPDATE carries all three
        conditions, so two deliveries racing cannot promote two claims.

        None for everything else, and the caller drops the delivery in
        silence: an installation nobody claimed, a claim that expired, a claim
        somebody else's workspace holds, or an action that proves nothing.
        """
        workspace_id = (
            await self._repository.find_confirmed_workspace_by_installation_id(
                connection,
                installation_id=installation_id,
            )
        )

        if workspace_id is not None:
            return workspace_id

        if event != "installation" or action not in CONFIRMING_ACTIONS:
            return None

        return await self._repository.confirm_installation(
            connection,
            installation_id=installation_id,
            within=CLAIM_TTL,
        )

    async def _apply_account(
        self,
        connection: asyncpg.Connection,
        scope: WorkspaceScope,
        installation: Mapping[str, Any],
    ) -> None:
        """Fill in the account login this installation lives in.

        The only source there is. The setup redirect carries an installation
        id and nothing else, so until a delivery arrives the column is NULL --
        see migrations/013_github_integration.sql.
        """
        account = installation.get("account")

        if not isinstance(account, Mapping):
            return

        login = account.get("login")

        if not isinstance(login, str) or not login:
            return

        await self._repository.set_account_login(
            connection,
            scope=scope,
            account_login=login,
        )

    async def _apply_full_set(
        self,
        connection: asyncpg.Connection,
        scope: WorkspaceScope,
        payload: Mapping[str, Any],
    ) -> None:
        """Replace the repository set with the one this payload lists.

        `installation.created` carries the whole set; the other actions of
        that event carry none, and then `_repositories` answers None and the
        stored set is left alone rather than emptied.
        """
        repositories = _repositories(payload.get("repositories"))

        if repositories is None:
            return

        await self._repository.delete_repositories(connection, scope=scope)
        await self._repository.add_repositories(
            connection,
            scope=scope,
            repositories=repositories,
        )

    async def _apply_delta(
        self,
        connection: asyncpg.Connection,
        scope: WorkspaceScope,
        payload: Mapping[str, Any],
    ) -> None:
        """Apply an `installation_repositories` add/remove pair.

        The added ids are deleted before they are inserted, alongside the
        removed ones. That is what makes a redelivery -- GitHub retries, and
        an at-least-once delivery is the only kind there is -- land on the
        same state instead of a primary key violation, and it is also how a
        rename in the same payload rewrites `full_name`.
        """
        added = _repositories(payload.get("repositories_added")) or ()
        removed = _repositories(payload.get("repositories_removed")) or ()

        stale = [repository.repository_id for repository in (*added, *removed)]

        if stale:
            await self._repository.delete_repositories(
                connection,
                scope=scope,
                repository_ids=stale,
            )

        await self._repository.add_repositories(
            connection,
            scope=scope,
            repositories=added,
        )

    def _view(
        self,
        installation: GithubInstallationEntity | None,
        repositories: Sequence[GithubRepositoryEntity],
    ) -> GithubIntegrationEntity:
        """Assemble the answer, with the status decided in one place.

        UNCONFIGURED wins over everything when a deployment's credentials have
        been removed while a workspace still holds an installation row. The
        row is still reported -- it is real, and a user is entitled to see
        what their workspace connected -- but the status says the integration
        cannot currently work, which is the honest answer and the one that
        stops a UI offering a Disconnect flow as a fix for a missing key.

        PENDING is the row existing without `confirmed_at`, and it must not
        collapse into either neighbour. Reporting it as CONNECTED is the
        original defect -- a claim rendered as a fact. Reporting it as
        DISCONNECTED would be a different lie in the safe direction: the row
        does exist, it does hold the installation id against every other
        workspace, and a user told "not connected" would keep clicking Connect
        at a claim that is already theirs.
        """
        if not self._config.configured:
            status = UNCONFIGURED
        elif installation is None:
            status = DISCONNECTED
        elif installation.confirmed_at is None:
            status = PENDING
        else:
            status = CONNECTED

        return GithubIntegrationEntity(
            status=status,
            installation=installation,
            repositories=tuple(repositories),
        )
