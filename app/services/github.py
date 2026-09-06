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
from typing import Any, Final
from urllib.parse import urlsplit

import asyncpg
from pydantic import SecretStr

from app.config import Settings
from app.domain.errors import (
    GithubNotConfiguredError,
    WorkspaceAccessDeniedError,
)
from app.domain.github import (
    CONNECTED,
    DISCONNECTED,
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
            private_key=_secret(settings.github_app_private_key),
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
    ):
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

    async def connect(
        self,
        scope: AuthorizedWorkspaceScope,
        *,
        installation_id: int,
    ) -> GithubIntegrationEntity:
        """Record that this workspace has installed the app.

        `connected_by` comes from the scope and never from an argument, for
        the reason MembershipService gives about `user_id`: an argument is
        something a caller can choose, and this column is the answer to "who
        gave this app access to our code".

        Re-connecting replaces rather than merges. A workspace that installs
        the app into a different account must not keep the previous account's
        repositories, so the whole set goes first -- inside the same
        transaction, so there is no instant at which the workspace is
        connected to an account and holding another one's repository names.

        Raises GithubNotConfiguredError before touching the database. A row
        written by a deployment that cannot verify a webhook is a row nothing
        will ever fill in or clean up. GithubInstallationClaimedError comes
        from the repository and is left to propagate: another workspace
        holding this installation is not something this layer can resolve.
        """
        require_workspace_admin(scope)

        if not self._config.configured:
            raise GithubNotConfiguredError()

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.delete_repositories(connection, scope=scope)
                await self._repository.delete_installation(connection, scope=scope)

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
            # half-applied, and the workspace lookup has to hold for the
            # writes that follow it.
            async with connection.transaction():
                workspace_id = await self._repository.find_workspace_by_installation_id(
                    connection,
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

        UNCONFIGURED wins over CONNECTED when a deployment's credentials have
        been removed while a workspace still holds an installation row. The
        row is still reported -- it is real, and a user is entitled to see
        what their workspace connected -- but the status says the integration
        cannot currently work, which is the honest answer and the one that
        stops a UI offering a Disconnect flow as a fix for a missing key.
        """
        if not self._config.configured:
            status = UNCONFIGURED
        elif installation is None:
            status = DISCONNECTED
        else:
            status = CONNECTED

        return GithubIntegrationEntity(
            status=status,
            installation=installation,
            repositories=tuple(repositories),
        )
