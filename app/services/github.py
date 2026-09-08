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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
import httpx
from pydantic import SecretStr

from app.config import Settings
from app.domain.activity import ActivityKind
from app.domain.errors import (
    GithubNotConfiguredError,
    GithubRepositoriesInUseError,
    ValidationError,
    ValidationIssue,
    WorkspaceAccessDeniedError,
)
from app.domain.events import DomainEventKind
from app.domain.github import (
    CAUSE_MAX_LENGTH,
    CONNECTED,
    DEVELOPMENT_LIMIT,
    DISCONNECTED,
    LINK_SOURCE_BODY,
    LINK_SOURCE_BRANCH,
    LINK_SOURCE_TITLE,
    NO_GRANT,
    PENDING,
    PULL_REQUEST_STATES,
    UNCONFIGURED,
    GithubAutomationEntity,
    GithubDevelopmentEntity,
    GithubGrant,
    GithubInstallationEntity,
    GithubIntegrationEntity,
    GithubRepositoryEntity,
    automated_move_for,
    branch_name_for,
    categories_below,
    issue_identifiers,
    pull_request_cause,
    pull_request_display_state,
)
from app.domain.notifications import NotificationKind
from app.domain.tenancy import AuthorizedWorkspaceScope, WorkspaceScope
from app.repositories.github import GithubRepository
from app.services import activity
from app.services.events import record_issue_event, record_pull_request_merged


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

# The events this server has a rule for. Everything else is accepted and
# ignored, because a non-2xx earns a redelivery for a payload that will never
# be handled differently.
#
# Checked before a connection is acquired, so `ping`, `check_run`,
# `workflow_job` and the rest of what a GitHub App is subscribed to cost this
# process one set membership each and no database work at all.
HANDLED_EVENTS: Final = frozenset(
    {"installation", "installation_repositories", "pull_request", "push"}
)

# The ceiling `github_deliveries_delivery_id_length` puts on the header.
#
# An id longer than this is not one GitHub sent -- it sends 36 -- and writing
# it would abort the delivery on the CHECK, which is a 500 and therefore an
# unbounded redelivery loop. Over-long ids are treated as absent instead: the
# delivery is applied without the idempotency guard rather than refused
# forever. See `_claim_delivery`.
DELIVERY_ID_MAX_LENGTH: Final = 200

# github_pull_requests_title_length, restated so the value is clamped here
# rather than refused by the database mid-delivery.
PULL_REQUEST_TITLE_MAX_LENGTH: Final = 1024

# github_commits_message_length. A commit message can genuinely be long; this
# is the point past which it is truncated rather than the delivery lost.
COMMIT_MESSAGE_MAX_LENGTH: Final = 8192

# github_pull_requests_url_length and github_commits_url_length.
URL_MAX_LENGTH: Final = 2048

# github_pull_requests_head_ref_format: printable ASCII, no spaces, bounded.
# A ref that does not match is dropped to NULL rather than stored, for the
# same reason the lengths above are clamped -- a CHECK violation inside the
# delivery's transaction is a redelivery loop.
_HEAD_REF = re.compile(r"^[!-~]{1,255}$")

# github_commits_sha_format: exactly 40 lowercase hex characters.
_SHA = re.compile(r"^[0-9a-f]{40}$")

# How many commits one push may contribute.
#
# GitHub caps its own `commits` array at 20 and sends `head_commit` beside it,
# so this is reached only by a payload that has grown a new shape. A bound on
# work rather than a product rule: each commit costs an insert and a link
# write inside the delivery's transaction.
COMMITS_PER_PUSH_LIMIT: Final = 50


# What a team is told when it asks for the derived default and has no state to
# derive one from.
#
# `teamId` rather than a state field, because there is no state field to blame:
# the caller named nothing, and what is wrong is the team's board. 005 seeds
# every team with one state of each category, so this is a team that deleted
# them -- rare, and much better answered than silently doing nothing.
_NO_DEFAULT_STATES = ValidationIssue(
    field="teamId",
    code="NO_DEFAULT_STATES",
    message=(
        "This team has no started or completed workflow state to automate to. "
        "Add one, or choose the states explicitly."
    ),
)


def _wrong_state(field: str, category: str) -> ValidationIssue:
    """A chosen state that is not this team's, or not of the right category.

    ONE issue for both, and deliberately so. "That state belongs to another
    team" and "that state is a backlog state" are different mistakes, but
    telling them apart tells a caller that an id they guessed names a real
    state somewhere -- and workflow states are readable only through a
    workspace the caller is a member of. A typo and a probe get the same
    answer, which is the rule `WorkspaceAccessDeniedError` states for the
    workspace itself.
    """
    return ValidationIssue(
        field=field,
        code="INVALID_STATE",
        message=(f"Choose a workflow state of this team whose category is {category}."),
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


# --- reading a development payload --------------------------------------
#
# Every function below turns one JSON value into something the schema in
# migrations/017_github_development.sql will accept, or into None. None is
# always a value the caller can act on, and never an exception, for one
# reason that applies to all of them: these run inside the delivery's
# transaction, and a CHECK violation or a raised parse error aborts it -- so
# GitHub sees a 500, redelivers, and fails again, forever. Clamping and
# dropping here is what makes a payload GitHub changes the shape of a missing
# field rather than an outage.
#
# The payload is already proven to be GitHub's by the signature. That makes
# these shape checks rather than trust boundaries -- with one exception, which
# is the whole of the feature: the TEXT inside a pull request is written by
# whoever opened it, and no signature says anything about that. It is never
# trusted here and never resolved here; see `_link_pull_request`.


def _text(value: Any, limit: int) -> str | None:
    """A non-empty string, clamped to a column's length. None otherwise.

    Truncated rather than refused, because the length is the database's bound
    and not a statement about the payload: a title one character over is still
    the pull request's title, and losing the whole delivery over the last
    character would be a worse answer than losing the last character.
    """
    if not isinstance(value, str):
        return None

    trimmed = value.strip()[:limit].strip()

    return trimmed or None


def _url(value: Any) -> str | None:
    """The html_url GitHub reported, if it fits the column."""
    return _text(value, URL_MAX_LENGTH)


def _head_ref(value: Any) -> str | None:
    """A branch name that matches github_pull_requests_head_ref_format."""
    if not isinstance(value, str) or not _HEAD_REF.match(value):
        return None

    return value


def _sha(value: Any) -> str | None:
    """A full 40-character lowercase SHA-1, or None.

    Lowercased before matching, because git and GitHub both accept either
    case and the column is keyed on one. A repository on SHA-256 sends 64
    characters and lands here as None -- which is the right failure: widening
    the column is a migration with an index and a display story, not something
    to accept silently into a value the UI abbreviates to seven characters.
    """
    if not isinstance(value, str):
        return None

    lowered = value.lower()

    return lowered if _SHA.match(lowered) else None


def _instant(value: Any) -> datetime | None:
    """One of GitHub's ISO 8601 timestamps as an aware datetime, or None.

    `Z` is rewritten because `datetime.fromisoformat` accepts it only from
    3.11 onwards and the two spellings mean the same instant; a value with no
    offset at all is read as UTC, which is what GitHub sends everywhere it
    omits one.

    Aware rather than naive, deliberately. These are compared against
    `github_updated_at` in SQL to decide whether a redelivery is older than
    what is stored, and a naive value in that comparison is a comparison
    against whatever timezone the session happens to be in.
    """
    if not isinstance(value, str):
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed


def _payload_repository_id(payload: Mapping[str, Any]) -> int | None:
    """Which repository this delivery is about, by GitHub's numeric id.

    The id and never `full_name`: the name is what a rename rewrites, and a
    delivery that arrived between a rename and the `repository` event that
    reports it would otherwise match nothing.

    Answering this does NOT establish that the repository belongs to the
    workspace the delivery routed to. That is a separate question, asked
    against `github_repositories` under the resolved scope, and the two must
    stay separate: `github_repositories_repository_id_idx` is deliberately not
    unique, because two organisations may both grant access to a fork.
    """
    repository = payload.get("repository")

    if not isinstance(repository, Mapping):
        return None

    return _positive_int(repository.get("id"))


def _payload_repository_name(payload: Mapping[str, Any]) -> str | None:
    """`owner/name`, for the sentence the history writes about an automated move.

    Read from the payload rather than from `github_repositories`, and safely so
    for one reason: `repository_exists` has already established, under this
    workspace's scope, that the id belongs here -- and the name arrives in the
    same signature-verified body as the id. It costs no round trip inside a
    delivery's transaction, which the stored copy would.

    The value is a rendering and never an identity: nothing looks a repository
    up by it, `_payload_repository_id` is what routes the delivery, and a
    workspace that renamed the repository between two deliveries gets two
    timeline rows naming it two ways -- which is what actually happened.
    """
    repository = payload.get("repository")

    if not isinstance(repository, Mapping):
        return None

    return _text(repository.get("full_name"), CAUSE_MAX_LENGTH)


@dataclass(frozen=True, slots=True)
class _PushedCommit:
    """One commit off a push payload, already checked against the schema.

    A parse result rather than a domain entity: it holds the identifiers its
    message asked to link, which is a step in applying a delivery and not a
    fact about a commit. `GithubCommitEntity` is what a reader gets back.
    """

    sha: str
    message: str
    url: str | None
    committed_at: datetime | None
    identifiers: tuple[tuple[str, int], ...]


def _pushed_commit(value: Any) -> _PushedCommit | None:
    """One entry of a push payload's commit list, or None if it is not one."""
    if not isinstance(value, Mapping):
        return None

    sha = _sha(value.get("id"))
    message = _text(value.get("message"), COMMIT_MESSAGE_MAX_LENGTH)

    if sha is None or message is None:
        return None

    return _PushedCommit(
        sha=sha,
        message=message,
        url=_url(value.get("url")),
        committed_at=_instant(value.get("timestamp")),
        identifiers=issue_identifiers(message),
    )


def _pushed_commits(payload: Mapping[str, Any]) -> tuple[_PushedCommit, ...]:
    """Every commit this push reports, de-duplicated by SHA.

    `head_commit` is read alongside the list and not instead of it. GitHub
    caps `commits` at twenty entries, so on a large push the tip -- which is
    where a merge commit's "Fixes ENG-142" lives -- is in `head_commit` and
    nowhere else. On an ordinary push it is the last element of the list as
    well, which is why the two are merged by SHA rather than concatenated.

    Malformed entries are skipped rather than raising, for the reason
    `_repositories` gives: the payload is already proven to be GitHub's, so a
    bad entry is a schema change and not an attack -- and raising would fail
    the whole delivery, which GitHub then retries, which fails again.
    """
    listed = payload.get("commits")
    entries = list(listed) if isinstance(listed, list) else []
    entries.append(payload.get("head_commit"))

    found: dict[str, _PushedCommit] = {}

    for entry in entries:
        commit = _pushed_commit(entry)

        if commit is None or commit.sha in found:
            continue

        found[commit.sha] = commit

        if len(found) >= COMMITS_PER_PUSH_LIMIT:
            break

    return tuple(found.values())


# --- proving the installer owns what they claimed -----------------------

# GitHub's OAuth token endpoint, and the API root the resulting user token is
# spent against. Module constants and never arguments, so no caller can aim
# this exchange at a host of their choosing.
GITHUB_TOKEN_URL: Final = "https://github.com/login/oauth/access_token"
GITHUB_USER_INSTALLATIONS_URL: Final = "https://api.github.com/user/installations"

# The repositories one installation covers, as the INSTALLER sees them.
#
# The user-to-server route rather than the app-to-server one
# (`/installation/repositories` signed with the app's private key), because
# this flow already holds a user token and holds no key: minting an
# installation token needs RS256 JWT signing, which this application
# deliberately does not carry -- see the note in migrations/013.
GITHUB_INSTALLATION_REPOSITORIES_URL: Final = (
    "https://api.github.com/user/installations/{installation_id}/repositories"
)

# One page. An installation covering more than this is a case this flow does
# not have, and the webhook path fills in anything a first page missed.
REPOSITORY_PAGE_SIZE: Final = 100

# How long the two calls below may take. Short, because they sit inside an
# OAuth callback the user is watching, and a provider that has stopped
# answering must not hold the request open.
VERIFY_TIMEOUT_SECONDS: Final = 10.0


def _resolve_installation(
    installation_ids: tuple[int, ...], preferred: int | None
) -> int | None:
    """Which installation's repositories are worth one request, if any.

    The id the redirect named, when the grant actually lists it -- an id it
    does not list is a guess, and enumerating a guess would ask GitHub about
    somebody else's installation. Failing that, the sole installation, because
    with exactly one there is nothing to choose between.

    None for the ambiguous case: several installations and no id naming one.
    The callback refuses that too, so there is nothing to fetch repositories
    for and picking one would attach a repository set nobody asked for.

    Deliberately mirrors, rather than shares, the callback's own rule about
    which id to CLAIM. The two answer different questions -- what to ask
    GitHub, and what to write down -- and the cost of them disagreeing is a
    wasted request and an empty set, never a wrong row.
    """
    if preferred is not None and preferred in installation_ids:
        return preferred

    if len(installation_ids) == 1:
        return installation_ids[0]

    return None


class InstallationOwnershipCheck(Protocol):
    """Which installations the person finishing this flow can administer."""

    async def installations_for_user(
        self, *, code: str, preferred: int | None = None
    ) -> GithubGrant: ...


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

    async def installations_for_user(
        self, *, code: str, preferred: int | None = None
    ) -> GithubGrant:
        """Every installation the consenting account administers, and its repos.

        Returns ids rather than answering a yes/no about one, because the
        redirect does not always name an installation. GitHub sends
        `installation_id` when the click INSTALLED the app; for an app already
        installed there is nothing to install, so the redirect carries only
        `code` -- and the list is then the only way to learn which
        installation the person just authorised against.

        The code is single-use, so this must be called at most once per
        callback and its result reused for every question the caller has:
        which installation, whether a named one is really theirs, and -- since
        the same exchange is the only chance to ask -- what that installation
        covers.

        `preferred` is the id the redirect named, or None. It decides which
        installation's repositories are worth a request, and nothing else: the
        caller still settles which id to CLAIM from `installation_ids`, on its
        own rules. Resolving to a different one than the caller does costs a
        wasted request and an empty repository set, never a wrong write.

        Repositories are fetched at most once. An account with several
        installations and no `installation_id` in the redirect is ambiguous,
        the caller refuses it anyway, and guessing one to enumerate would be
        asking GitHub about an installation nobody chose.
        """
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
                return NO_GRANT

            token = granted.json().get("access_token")

            if not isinstance(token, str) or not token:
                return NO_GRANT

            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            # One page is enough for the question being asked; an account with
            # more than a hundred installations is not a case this flow has,
            # and asking for more would turn a verification into a crawl.
            listed = await client.get(
                GITHUB_USER_INSTALLATIONS_URL,
                headers=headers,
                params={"per_page": REPOSITORY_PAGE_SIZE},
            )

            if listed.status_code != 200:
                return NO_GRANT

            installation_ids = tuple(
                entry["id"]
                for entry in (listed.json().get("installations") or [])
                if isinstance(entry.get("id"), int)
            )

            resolved = _resolve_installation(installation_ids, preferred)

            if resolved is None:
                return GithubGrant(installation_ids=installation_ids, repositories=())

            covered = await client.get(
                GITHUB_INSTALLATION_REPOSITORIES_URL.format(installation_id=resolved),
                headers=headers,
                params={"per_page": REPOSITORY_PAGE_SIZE},
            )

        if covered.status_code != 200:
            # The installation is still real and the claim still confirmable;
            # only the repository list is missing. Returning the ids without
            # them degrades to exactly the behaviour that shipped before this
            # call existed -- an integration whose repositories arrive with the
            # next delivery -- rather than failing a connect that is otherwise
            # complete.
            return GithubGrant(installation_ids=installation_ids, repositories=())

        # `_repositories` is the webhook path's parser, reused rather than
        # copied: this endpoint answers `{"repositories": [...]}` with entries
        # of the same shape, and a second parser would be a second place for
        # "skip the malformed, collapse duplicate ids" to drift.
        return GithubGrant(
            installation_ids=installation_ids,
            repositories=_repositories(covered.json().get("repositories")) or (),
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

            # Read whether or not there is an installation, unlike the
            # repositories above. An automation is configuration a team wrote
            # about its own board, and it survives the integration being
            # disconnected and reconnected -- `github_issue_automations`
            # references `teams` and `workflow_states`, never
            # `github_installations`, precisely so that reconnecting does not
            # silently discard every team's settings. Hiding it here would tell
            # an admin their configuration was gone when it was waiting.
            automations = await self._repository.list_automations(
                connection,
                scope=scope,
            )

        return self._view(installation, repositories, automations)

    async def installations_for_grant(
        self, *, code: str, preferred: int | None = None
    ) -> GithubGrant:
        """What the consenting account administers, and what it covers.

        Exposed on the service so the callback asks GitHub exactly once: the
        OAuth code is single-use, and the callback has three questions for it
        -- which installation this authorisation is about, whether an id the
        redirect named is genuinely the caller's, and which repositories that
        installation covers.

        Fails closed to an empty grant, so every caller treats a provider that
        is down, slow, or answering nonsense the same way it treats an account
        with nothing installed.
        """
        if self._ownership is None:
            return NO_GRANT

        try:
            return await self._ownership.installations_for_user(
                code=code, preferred=preferred
            )
        except Exception:
            # A provider failure must not become a 500 in the middle of an
            # OAuth callback. PENDING is an honest state; the webhook path and
            # the claim TTL both still apply.
            return NO_GRANT

    async def confirm_claim(
        self,
        *,
        installation_id: int,
        repositories: Sequence[GithubRepositoryEntity] = (),
    ) -> bool:
        """Promote a claim GitHub has confirmed belongs to the installer.

        The second route to `confirmed_at`, and the one that makes connecting
        to an ALREADY-INSTALLED app possible at all: GitHub emits
        `installation.created` once, so a workspace joining an existing
        installation would otherwise wait for a delivery that never arrives
        and sit at PENDING until the claim expired.

        The evidence differs from the webhook's but is not weaker. A signed
        delivery is GitHub saying an installation happened; the OAuth grant is
        GitHub saying THIS account administers THIS installation, which is a
        closer answer to the question a claim actually poses -- who clicked.

        Callers must have established that from `installations_for_grant`
        before calling this; it is the write, not the check.
        `confirm_installation` remains the only writer of `confirmed_at`, with
        all three of its predicates, so a claim already confirmed, already
        expired, or belonging to nobody is untouched.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                workspace_id = await self._repository.confirm_installation(
                    connection,
                    installation_id=installation_id,
                    within=CLAIM_TTL,
                )

                # Seeded in the SAME transaction as the promotion, and only
                # when there was one. `confirm_installation` answers the
                # workspace whose claim it just promoted, so the repositories
                # land on exactly that tenant without this method taking a
                # scope it could be handed the wrong one of.
                #
                # This is what makes connecting to an already-installed app
                # produce a usable integration. GitHub emits `installation`
                # once, at first install, so a reconnect -- disconnect inside
                # Vector, press Connect again, with the app still on the
                # account -- has no delivery to wait for and used to arrive
                # CONNECTED covering nothing, permanently. The grant is the
                # same evidence that promotes the claim, so a repository set
                # read from it is trusted exactly as far.
                #
                # `connect` has already deleted this workspace's repositories
                # inside its own transaction, so there is nothing here for
                # `add_repositories` to collide with -- which matters, because
                # it has no ON CONFLICT.
                if workspace_id is not None and repositories:
                    await self._repository.add_repositories(
                        connection,
                        scope=WorkspaceScope(workspace_id=workspace_id),
                        repositories=repositories,
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
        repositories -- nor the pull requests and commits hanging off them,
        which is why the development rows go first and not merely because
        RESTRICT would refuse the order the other way round. All of it inside
        the same transaction, so there is no instant at which the workspace is
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
                await self._repository.delete_development(connection, scope=scope)

                try:
                    await self._repository.delete_repositories(connection, scope=scope)
                except asyncpg.RestrictViolationError:
                    # Reconnecting replaces rather than merges, so it runs the
                    # same delete disconnect does and earns the same refusal.
                    raise GithubRepositoriesInUseError() from None

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

        Children first, and there are three generations of them now: the two
        link tables, then the pull requests and commits, then the
        repositories, then the installation. Every foreign key in 013 and 017
        is RESTRICT, so any other order is a RestrictViolationError -- which is
        what RESTRICT is for. It refuses to let one delete quietly discard
        development history while the command tag reads `DELETE 1`, and makes
        the ordering a decision this method states rather than one the schema
        performs invisibly.

        All of it in one transaction, so a failure part way cannot leave a
        pull request belonging to a repository that is gone.
        """
        require_workspace_admin(scope)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.delete_development(connection, scope=scope)

                try:
                    await self._repository.delete_repositories(connection, scope=scope)
                except asyncpg.RestrictViolationError:
                    # Something outside this integration still references the
                    # repositories -- 024's releases are the first, and are
                    # RESTRICT on purpose. Uncaught, that reached the client as
                    # "Internal server error" for a refusal the schema made
                    # deliberately and the admin can act on.
                    raise GithubRepositoriesInUseError() from None

                await self._repository.delete_installation(connection, scope=scope)

        return self._view(None, ())

    async def set_tracked_repositories(
        self,
        scope: AuthorizedWorkspaceScope,
        *,
        repository_ids: Sequence[int],
    ) -> GithubIntegrationEntity:
        """Choose which of the installation's repositories this workspace tracks.

        An installation covering two hundred repositories is not a workspace
        that wants development activity from two hundred repositories, and this
        is the setting that says so. It is a NARROWING of what GitHub already
        granted and never a widening: the statement is an UPDATE over rows this
        workspace already has, so an id naming a repository the installation
        does not cover matches nothing and is silently ignored rather than
        refused -- which is also what stops this doubling as an oracle for
        whether some other tenant covers a repository.

        The whole set in one call, because that is what a page of checkboxes
        submits. Two admins saving different selections is a last-writer-wins
        race, which is the same race every settings form has and the right one
        here: the alternative -- per-repository toggles -- would let two saves
        interleave into a set neither admin chose.

        Untracking a repository a RELEASE names is refused, and that refusal is
        this method's own rather than the schema's. `releases_repository_fk` is
        RESTRICT and stops `connect` and `disconnect`, which DELETE the rows;
        untracking is a boolean flip and trips no constraint at all. The rule
        migration 024 argues for is the same either way -- shipping history
        must not be quietly detached by a toggle elsewhere -- so it is checked
        here, in the same transaction, and reported as the error an admin can
        act on rather than as a silently broken release page.

        Untracking does NOT delete the development history already collected.
        See migration 030: that history was gathered while the repository was
        tracked and is what an issue's Development section is showing. What
        stops is new deliveries.
        """
        require_workspace_admin(scope)

        wanted = list(dict.fromkeys(repository_ids))

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                covered = await self._repository.list_repositories(
                    connection,
                    scope=scope,
                )

                dropped = [
                    repository.repository_id
                    for repository in covered
                    if repository.repository_id not in set(wanted)
                ]

                if await self._repository.repositories_with_releases(
                    connection,
                    scope=scope,
                    repository_ids=dropped,
                ):
                    raise GithubRepositoriesInUseError()

                await self._repository.set_tracked_repositories(
                    connection,
                    scope=scope,
                    repository_ids=wanted,
                )

                repositories = await self._repository.list_repositories(
                    connection,
                    scope=scope,
                )
                installation = await self._repository.get_installation(
                    connection,
                    scope=scope,
                )
                automations = await self._repository.list_automations(
                    connection,
                    scope=scope,
                )

        return self._view(installation, repositories, automations)

    async def set_issue_automation(
        self,
        scope: AuthorizedWorkspaceScope,
        *,
        team_id: UUID,
        enabled: bool,
        started_state_id: UUID | None = None,
        completed_state_id: UUID | None = None,
    ) -> GithubIntegrationEntity:
        """Say what a pull request does to this team's issues.

        `enabled=False` removes the row, which is what off IS -- see migration
        030 on why there is no boolean column. Idempotent: turning off an
        automation that was never on succeeds.

        `enabled=True` with neither state named is the DEFAULT DERIVED FROM
        CATEGORY, and it is resolved exactly here and nowhere else. The team's
        first `started` state and first `completed` state by board order become
        the stored values, so what an admin turned on is visible on the settings
        page as two named states rather than as a rule that will pick something
        later. That is the whole answer to "which of a team's three started
        states": at configuration time a default is a suggestion an admin can
        see and change; at delivery time it would be a guess nobody made.

        A team with no state of a category gets NULL for that half and that
        trigger then does nothing -- honest, and better than reaching for a
        state of some other category.

        Naming a state explicitly is checked twice over, and both checks answer
        with a field error rather than an exception a client cannot act on:

        * it must be one of THIS team's states, which is also the tenancy check
          -- another tenant's state id resolves to nothing and is reported
          exactly as a typo is, so this cannot confirm that a leaked id exists;
        * its category must match the slot. A `started` slot pointing at a
          backlog state would move issues BACKWARDS the first time somebody
          opened a pull request, and `categories_below` -- which reads the
          target's category to decide what may move -- would then refuse every
          move and leave an automation that silently never fires.

        Refusing `enabled=True` with a state named for neither slot is not an
        arbitrary rule: `github_issue_automations_moves_something` refuses that
        row, so the alternative is a CheckViolationError reaching a client as
        "Internal server error" for a form somebody filled in half of.
        """
        require_workspace_admin(scope)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                if not enabled:
                    await self._repository.delete_automation(
                        connection,
                        scope=scope,
                        team_id=team_id,
                    )
                else:
                    await self._store_automation(
                        connection,
                        scope=scope,
                        team_id=team_id,
                        started_state_id=started_state_id,
                        completed_state_id=completed_state_id,
                    )

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

                automations = await self._repository.list_automations(
                    connection,
                    scope=scope,
                )

        return self._view(installation, repositories, automations)

    async def _store_automation(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        started_state_id: UUID | None,
        completed_state_id: UUID | None,
    ) -> None:
        """Validate and write one team's automation, on the caller's connection.

        Split out so `set_issue_automation` reads as the four things it does --
        authorize, write, read back, view -- rather than as one method with the
        category rules inlined into the middle of it.
        """
        if started_state_id is None and completed_state_id is None:
            started_state_id = await self._repository.default_state_id(
                connection,
                scope=scope,
                team_id=team_id,
                category="started",
            )
            completed_state_id = await self._repository.default_state_id(
                connection,
                scope=scope,
                team_id=team_id,
                category="completed",
            )

            if started_state_id is None and completed_state_id is None:
                # A team with neither a started nor a completed state has no
                # automation to derive, and writing a row of two NULLs is the
                # one thing the CHECK refuses. Reported rather than silently
                # doing nothing: the admin pressed a button.
                raise ValidationError([_NO_DEFAULT_STATES])

            await self._repository.upsert_automation(
                connection,
                scope=scope,
                team_id=team_id,
                started_state_id=started_state_id,
                completed_state_id=completed_state_id,
            )

            return

        slots = (
            ("startedStateId", "started", started_state_id),
            ("completedStateId", "completed", completed_state_id),
        )

        categories = await self._repository.find_states(
            connection,
            scope=scope,
            team_id=team_id,
            state_ids=[state_id for _, _, state_id in slots if state_id is not None],
        )

        issues = [
            _wrong_state(field, category)
            for field, category, state_id in slots
            if state_id is not None and categories.get(state_id) != category
        ]

        if issues:
            raise ValidationError(issues)

        await self._repository.upsert_automation(
            connection,
            scope=scope,
            team_id=team_id,
            started_state_id=started_state_id,
            completed_state_id=completed_state_id,
        )

    async def apply_webhook(
        self,
        *,
        event: str,
        payload: Mapping[str, Any],
        delivery_id: str | None = None,
    ) -> None:
        """Apply one verified delivery. Returns nothing, whatever happened.

        The caller has already verified GitHub's signature over the raw body;
        this method must never be reached by an unverified payload, which is
        why it takes the parsed mapping rather than the bytes -- parsing is
        what happens *after* verification, and a method that took bytes could
        be called in the wrong order.

        Silence is the contract. A delivery naming an installation no
        workspace has connected is ordinary -- the app is installed, the admin
        never finished the callback -- and so is an event this server has no
        rule for, and so is a redelivery of one already applied. Reporting any
        of them back to GitHub as a failure would earn a redelivery loop for
        something that will never succeed.

        Four events are handled. Two change what the settings page shows --
        `installation` (the account, the initial repository set, and removal)
        and `installation_repositories` (the set changing later) -- and two
        are the development activity an issue's panel renders,
        `pull_request` and `push`. Everything else is accepted and ignored.

        `delivery_id` is the X-GitHub-Delivery header, and it is claimed as
        the FIRST statement of the transaction below -- before the routing
        lookup, and therefore before anything is applied. GitHub's delivery is
        at-least-once by design: it retries anything it did not see a 2xx for,
        and `push` in particular writes rows a second application would have
        to be trusted to make idempotent one statement at a time. See
        `_claim_delivery`.

        This method is also where a claim becomes a connection, and that is
        deliberate rather than convenient: the signature checked upstream is
        the only evidence this deployment ever receives about who owns an
        installation, so the confirmation has to happen where the signature
        has just been verified and nowhere else. See `_resolve_workspace`.
        """
        if event not in HANDLED_EVENTS:
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
                if not await self._claim_delivery(
                    connection,
                    delivery_id=delivery_id,
                    event=event,
                ):
                    return

                workspace_id = await self._resolve_workspace(
                    connection,
                    event=event,
                    action=payload.get("action"),
                    installation_id=installation_id,
                )

                if workspace_id is None:
                    return

                scope = WorkspaceScope(workspace_id=workspace_id)

                await self._dispatch(
                    connection,
                    scope,
                    event=event,
                    payload=payload,
                    installation=installation,
                )

    async def _claim_delivery(
        self,
        connection: asyncpg.Connection,
        *,
        delivery_id: str | None,
        event: str,
    ) -> bool:
        """Whether this delivery is a first attempt. False means already done.

        The idempotency gate, run before the routing lookup and before any
        write. `github_deliveries` is not workspace-scoped and cannot be: the
        question "have I applied this already" has to be answerable before the
        lookup that would say whose it is, which is precisely the work a
        redelivery should not cost.

        A delivery with no id, or one longer than the column's bound, is
        applied unguarded rather than refused. A bounded write that fails the
        CHECK would abort the transaction and reach GitHub as a 500 -- an
        infinite redelivery loop for a header this server merely did not like.
        Unguarded means "as safe as the writes themselves are", which is what
        every delivery was before this table existed: the repository writes
        are upserts, and the link writes are set-valued.
        """
        if delivery_id is None or not 0 < len(delivery_id) <= DELIVERY_ID_MAX_LENGTH:
            return True

        return await self._repository.record_delivery(
            connection,
            delivery_id=delivery_id,
            event=event,
        )

    async def _dispatch(
        self,
        connection: asyncpg.Connection,
        scope: WorkspaceScope,
        *,
        event: str,
        payload: Mapping[str, Any],
        installation: Mapping[str, Any],
    ) -> None:
        """Route one routed delivery to the rule that applies it.

        `_apply_account` runs before any repository write, in the caller's one
        transaction, and that ordering is load-bearing beyond tidiness:
        `github_installations_unconfirmed_holds_no_account` in 016 aborts the
        whole delivery if this ever reached an unconfirmed row, and it does so
        before a private `full_name` from somebody else's organisation has
        been inserted.

        The two development events do NOT touch the account. They arrive for a
        confirmed installation by construction -- `_resolve_workspace` only
        confirms a claim from an `installation` event -- so there is nothing
        for them to fill in, and writing an account login from a payload that
        merely mentions one is a wider write than the event justifies.
        """
        if event == "pull_request":
            await self._apply_pull_request(connection, scope, payload)

            return

        if event == "push":
            await self._apply_push(connection, scope, payload)

            return

        if event == "installation" and payload.get("action") == "deleted":
            # The app was uninstalled on GitHub. The integration is over
            # whether or not anyone told Vector, so the rows go -- otherwise
            # the settings page reports CONNECTED for an installation that no
            # longer exists, and this workspace goes on holding development
            # history for repositories it can no longer see.
            #
            # Children first, in this order, because every foreign key in 013
            # and 017 is RESTRICT. All of it inside the caller's transaction,
            # so there is no instant at which a pull request survives the
            # repository it was opened on.
            await self._repository.delete_development(connection, scope=scope)
            await self._repository.delete_repositories(connection, scope=scope)
            await self._repository.delete_installation(connection, scope=scope)

            return

        await self._apply_account(connection, scope, installation)

        if event == "installation":
            await self._apply_full_set(connection, scope, payload)
        else:
            await self._apply_delta(connection, scope, payload)

    async def _apply_pull_request(
        self,
        connection: asyncpg.Connection,
        scope: WorkspaceScope,
        payload: Mapping[str, Any],
    ) -> None:
        """Store a pull request as GitHub reports it, and re-derive its links.

        Every action of the event runs the same code -- opened, edited,
        reopened, closed, converted_to_draft, ready_for_review, synchronize --
        because each carries the WHOLE `pull_request` object as it now stands,
        not a diff. So there is nothing to branch on: the row is written from
        the payload, and the links are re-derived from the payload's current
        title, body and head ref. That is what makes an edit retract what the
        edited text no longer says, without a single `if action ==` to get
        wrong.

        The repository is checked against this workspace's installation before
        anything is written. `github_pull_requests_repository_fk` would refuse
        the row anyway, but a foreign-key violation aborts the transaction and
        becomes a 500, which GitHub answers by redelivering forever.

        `merged_at` is cleared unless GitHub also says the pull request is
        closed. Not defensive tidiness: `github_pull_requests_merged_is_closed`
        refuses that combination, so a payload GitHub ever sent with both
        would take the delivery down rather than store a state the derived
        display state could not answer for.

        A stale payload writes nothing AND re-derives nothing. Returning early
        on `upsert_pull_request` answering False is the second half of the
        out-of-order defence: the links come from the title, so applying them
        from a payload too old to store would retract, from an older title,
        links the current title still supports.

        A MERGE additionally emits a domain event, and three things about that
        are deliberate.

        It goes through `app.services.events`, which knows nothing about Slack
        -- no import from this module reaches a Slack client, a channel or a
        bot token, and a grep proving that is a test. A webhook handler that
        could post directly is a handler where the preference toggle does not
        apply, the redelivery posts twice, the failure is invisible, and the
        HTTP call happens inside this transaction with a pool connection held.
        This module's job ends at recording that a merge happened.

        It runs AFTER the links are derived, because the event fans out over
        them: one row per Vector issue the pull request is about. Before them
        it would announce a merge to nobody, or -- worse, on a re-derivation --
        to whoever the previous title named.

        It is emitted on EVERY delivery that reports a merged pull request, not
        only the one that first reported it, and the duplicate suppression is a
        primary key rather than a condition here. GitHub sends the whole
        `pull_request` object on every later edit of an already-merged pull
        request, each under a delivery id `github_deliveries` has never seen, so
        a check of "did this payload change the merge" would have to be right
        about a provider's semantics forever. The key is right by construction.
        """
        pull = payload.get("pull_request")
        repository_id = _payload_repository_id(payload)

        if repository_id is None or not isinstance(pull, Mapping):
            return

        number = _positive_int(pull.get("number"))
        state = pull.get("state")
        title = _text(pull.get("title"), PULL_REQUEST_TITLE_MAX_LENGTH)

        if number is None or state not in PULL_REQUEST_STATES or title is None:
            return

        if not await self._repository.repository_exists(
            connection,
            scope=scope,
            repository_id=repository_id,
        ):
            return

        head = pull.get("head")
        head_ref = _head_ref(head.get("ref")) if isinstance(head, Mapping) else None

        # Named rather than inlined into the call below, because it is now read
        # twice: once as the column and once as the answer to "is this a merge".
        merged_at = _instant(pull.get("merged_at")) if state == "closed" else None

        applied = await self._repository.upsert_pull_request(
            connection,
            scope=scope,
            repository_id=repository_id,
            number=number,
            title=title,
            state=state,
            draft=pull.get("draft") is True,
            merged_at=merged_at,
            head_ref=head_ref,
            url=_url(pull.get("html_url")),
            github_updated_at=_instant(pull.get("updated_at")),
        )

        if not applied:
            return

        linked = await self._link_pull_request(
            connection,
            scope,
            repository_id=repository_id,
            number=number,
            title=title,
            body=pull.get("body"),
            head_ref=head_ref,
        )

        if merged_at is not None:
            await record_pull_request_merged(
                connection,
                scope=scope,
                repository_id=repository_id,
                number=number,
                title=title,
            )

        # The status automation, last, and on the SAME links the event above
        # fans out over.
        #
        # Inline rather than hung off the domain event that `record_pull_request
        # _merged` just wrote, and that is a decision worth stating because the
        # outbox is right there. Three reasons, in order of weight:
        #
        # * there is no event for a pull request OPENING, so half the feature
        #   would need a new kind in `domain_events_kind_known` -- and every
        #   kind in that vocabulary is a Slack preference toggle, so the
        #   automation would ship a switch in somebody's Slack settings for a
        #   message nobody asked to receive;
        # * `domain_events`' claim columns are Slack's (`slack_attempts`,
        #   `slack_next_attempt_at`). A second consumer needs a second set, a
        #   second background loop and its own backoff, bought to make a local
        #   write asynchronous. The outbox exists to keep a NETWORK call out of
        #   this transaction; a state move is a row in the same database;
        # * the automation PRODUCES into `domain_events` -- an issue it moves
        #   into a completed state emits `issue_completed`, so Slack announces
        #   it exactly as it announces a person's move. A consumer that writes
        #   into the table it consumes from is the shape to avoid.
        #
        # The single emitter the pipeline wants is preserved: `_automate_state`
        # calls `app.services.events.record_issue_event`, the same function
        # `activity.record_changes` calls for a human edit.
        await self._automate_state(
            connection,
            scope,
            action=payload.get("action"),
            state=state,
            draft=pull.get("draft") is True,
            merged_at=merged_at,
            issue_ids=linked,
            cause=pull_request_cause(_payload_repository_name(payload), number),
        )

    async def _automate_state(
        self,
        connection: asyncpg.Connection,
        scope: WorkspaceScope,
        *,
        action: Any,
        state: str,
        draft: bool,
        merged_at: datetime | None,
        issue_ids: Sequence[UUID],
        cause: str,
    ) -> None:
        """Move the issues this pull request names, if their teams asked for it.

        The rule is `app.domain.github.automated_move_for`, and it is stated
        there rather than here because it is a decision about pull requests and
        not about SQL: opened, reopened and ready_for_review move an issue to
        the team's started state; a merge moves it to the team's completed one;
        a draft and a close-without-merge move nothing.

        Everything else this method does exists because a state change is not
        one write. A person moving an issue through `IssueService.update` gets
        an activity row, a notification to the watchers and a domain event, all
        in the transaction that moved it -- see `activity.record_changes` -- and
        an automated move that produced only the UPDATE would be an issue that
        changed status with the timeline silent, nobody told, and no
        announcement in a channel that gets one for every human move. So the
        same three follow, through the same functions.

        LOGGED WITH A CAUSE AND NO ACTOR. `actor_id` is None because there is
        no person: a webhook runs on nobody's session, and inventing the
        installer as the actor would attribute to them a move they did not make
        and were possibly asleep for. `caused_by` is what migration 030 adds so
        that None does not read as "nobody knows" -- it names the pull request,
        which is the sentence the timeline has to be able to say.

        Idempotency and don't-fight-a-human are the same predicate and neither
        is here: `move_issues_for_automation` moves an issue only from a
        category BELOW the target, so a redelivery finds it already there and
        returns no rows, and a person who has moved it on themselves keeps
        their move. Nothing after this point runs for an issue that did not
        actually move.
        """
        target = automated_move_for(
            action=action,
            display_state=pull_request_display_state(
                state=state,
                draft=draft,
                merged_at=merged_at,
            ),
        )

        if target is None or not issue_ids:
            return

        moved = await self._repository.move_issues_for_automation(
            connection,
            scope=scope,
            issue_ids=issue_ids,
            category=target,
            from_categories=categories_below(target),
        )

        for issue_id, from_state_id, to_state_id in moved:
            await activity.record(
                connection,
                scope=scope,
                issue_id=issue_id,
                actor_id=None,
                kind=ActivityKind.STATE_CHANGED,
                from_value=str(from_state_id),
                to_value=str(to_state_id),
                caused_by=cause,
            )

            # The watchers, exactly as a human move notifies them. `actor_id`
            # is None here too, which the statement handles: `IS DISTINCT FROM`
            # against a NULL actor excludes nobody, so everyone watching hears
            # about it -- which is right, because nobody in this workspace did
            # it and there is therefore nobody to spare the notification.
            await activity.notify(
                connection,
                scope=scope,
                issue_id=issue_id,
                actor_id=None,
                kind=NotificationKind.STATUS_CHANGED,
            )

            # Writes a row only when the issue is NOW in a completed state --
            # the join is inside the statement, so the started move emits
            # nothing and the merge move emits exactly what a person's move to
            # the same state would.
            await record_issue_event(
                connection,
                scope=scope,
                kind=DomainEventKind.ISSUE_COMPLETED,
                issue_id=issue_id,
            )

    async def _link_pull_request(
        self,
        connection: asyncpg.Connection,
        scope: WorkspaceScope,
        *,
        repository_id: int,
        number: int,
        title: str,
        body: Any,
        head_ref: str | None,
    ) -> tuple[UUID, ...]:
        """Point this pull request at the issues its text asks for, per source.

        This is the method migrations/017_github_development.sql was written
        around, so the rule is worth stating where it is implemented: the
        identifier is text somebody wrote in a pull request, and on a public
        repository that somebody is anybody at all. It is a REQUEST to link
        and never a proof of one.

        The single line that makes it safe is that `scope` is the workspace
        that owns the repository this delivery came from, and it is the only
        workspace `resolve_issue_ids` is given. An identifier naming another
        tenant's team therefore resolves to nothing -- not to a row this code
        then declines to write, but to no id at all. 017's composite foreign
        keys are the floor under that rather than the mechanism: they make a
        mistake here unstorable, and this makes it unmade.

        Nothing about `body` is trusted for its content either. It is scanned
        by the same bounded parser as the title, and its links are stored
        under their own source, so a body edited by a commenter can add and
        remove only what a body may.

        All three sources are written on every delivery, including the ones
        that resolved to nothing -- that empty write is the retraction. A
        title edited to drop "ENG-142" makes `set_pull_request_links` delete
        the title's row, while the branch's row for the same issue survives
        because `source` is inside the key.

        Returns the DISTINCT issues this pull request now links to, across all
        three sources, which is the set the status automation acts on. Returned
        rather than re-read, because the resolution has just been performed and
        a second query for the same answer inside this transaction would be a
        round trip with a chance of disagreeing with the rows it just wrote.
        `dict.fromkeys` preserves first-appearance order, so a title naming two
        issues moves them in the order it named them and a redelivery does the
        same -- which matters only for the history's tie-break, and matters
        there.
        """
        by_source = {
            LINK_SOURCE_TITLE: issue_identifiers(title),
            LINK_SOURCE_BODY: issue_identifiers(
                body if isinstance(body, str) else None
            ),
            LINK_SOURCE_BRANCH: issue_identifiers(head_ref),
        }

        # One resolution for the union rather than three: the three sources
        # routinely name the same issue, and this runs inside the delivery's
        # transaction where a round trip costs a lock held longer.
        resolved = await self._repository.resolve_issue_ids(
            connection,
            scope=scope,
            identifiers=tuple(
                dict.fromkeys(
                    identifier
                    for identifiers in by_source.values()
                    for identifier in identifiers
                )
            ),
        )

        linked: list[UUID] = []

        for source, identifiers in by_source.items():
            issue_ids = [
                resolved[identifier]
                for identifier in identifiers
                if identifier in resolved
            ]

            await self._repository.set_pull_request_links(
                connection,
                scope=scope,
                repository_id=repository_id,
                number=number,
                source=source,
                issue_ids=issue_ids,
            )

            linked.extend(issue_ids)

        return tuple(dict.fromkeys(linked))

    async def _apply_push(
        self,
        connection: asyncpg.Connection,
        scope: WorkspaceScope,
        payload: Mapping[str, Any],
    ) -> None:
        """Record the commits a push reported, and what they say they are about.

        Additive and never retracting, which is the difference from the pull
        request above and follows from the data rather than from a policy: a
        commit message is immutable, so there is no source to re-derive and no
        edit that could withdraw a link. `github_commit_issues` carries no
        `source` column for the same reason.

        The same tenancy rule applies in full. A commit message is text its
        author wrote, the identifiers in it are resolved only within the
        workspace that owns the repository the push came from, and a message
        naming another tenant's ENG-142 resolves to nothing.

        A push that reports no usable commit -- a branch deletion, a tag, a
        payload whose entries are all malformed -- writes nothing and asks the
        database nothing.
        """
        repository_id = _payload_repository_id(payload)
        commits = _pushed_commits(payload)

        if repository_id is None or not commits:
            return

        if not await self._repository.repository_exists(
            connection,
            scope=scope,
            repository_id=repository_id,
        ):
            return

        resolved = await self._repository.resolve_issue_ids(
            connection,
            scope=scope,
            identifiers=tuple(
                dict.fromkeys(
                    identifier
                    for commit in commits
                    for identifier in commit.identifiers
                )
            ),
        )

        for commit in commits:
            await self._repository.add_commit(
                connection,
                scope=scope,
                repository_id=repository_id,
                sha=commit.sha,
                message=commit.message,
                url=commit.url,
                committed_at=commit.committed_at,
            )
            await self._repository.add_commit_links(
                connection,
                scope=scope,
                repository_id=repository_id,
                sha=commit.sha,
                issue_ids=[
                    resolved[identifier]
                    for identifier in commit.identifiers
                    if identifier in resolved
                ],
            )

    async def development_for_issue(
        self,
        scope: WorkspaceScope,
        *,
        issue_id: UUID,
        identifier: str,
        title: str,
    ) -> GithubDevelopmentEntity:
        """What one issue's Development section shows.

        Takes a plain `WorkspaceScope` and checks no role, unlike every other
        read here. That is deliberate and is the difference between the two
        halves of this feature: connecting a GitHub organisation is an admin's
        act and `require_workspace_admin` guards it, but the pull requests
        attached to an issue are part of the issue, and every member who can
        open the issue can see them. The caller's scope is what authorises
        this, and it comes from the resolver that already authorised the issue.

        The scope leads both statements, so an `issue_id` from another
        workspace answers with two empty lists -- the same answer an issue
        with no activity gets, which is what stops this confirming that a
        leaked id is real.

        `branch_name` is computed whatever the lists hold, because an issue
        with no development activity is exactly the one that needs it: the
        panel's first job is to offer the branch to start. It needs no GitHub
        permission and creates nothing -- it is a string to copy.

        A single read needs no write transaction, so this acquires a
        connection without opening one.
        """
        async with self._pool.acquire() as connection:
            pull_requests = await self._repository.list_pull_requests_for_issue(
                connection,
                scope=scope,
                issue_id=issue_id,
                limit=DEVELOPMENT_LIMIT,
            )
            commits = await self._repository.list_commits_for_issue(
                connection,
                scope=scope,
                issue_id=issue_id,
                limit=DEVELOPMENT_LIMIT,
            )

        return GithubDevelopmentEntity(
            branch_name=branch_name_for(identifier, title),
            pull_requests=tuple(pull_requests),
            commits=tuple(commits),
        )

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

        The development rows go with the repositories, and they have to: the
        set is replaced by a delete and an insert, and
        `github_pull_requests_repository_fk` is RESTRICT, so a repository with
        a pull request attached cannot be deleted while one exists. A
        repository that is in the new set as well as the old one loses its
        history here, which is the cost of replace-rather-than-merge; the
        alternative -- diffing the two sets to spare the survivors -- is more
        code on the path of an event that carries the whole set precisely so
        nobody has to.
        """
        repositories = _repositories(payload.get("repositories"))

        if repositories is None:
            return

        await self._repository.delete_development(connection, scope=scope)
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

        A repository REMOVED from the installation takes its development
        history with it, and that is the point rather than a side effect: the
        app no longer has access to that repository, so Vector must not go on
        showing the pull request titles and commit messages it collected while
        it did. This is the "access changed" case, and it is the one that
        actually fires -- an organisation narrowing an installation's
        repository list sends `installation_repositories.removed` and nothing
        else.

        The development delete covers the added ids too, for the plainer
        reason that they are being deleted and re-inserted: RESTRICT would
        refuse the repository delete otherwise. An id that was already there
        is one this payload is re-stating, so there is nothing being lost that
        the next delivery does not restore.

        ponytail: the delete-and-insert resets `tracked` to its default for
        every id in the payload, so a repository an admin had UNTRACKED and
        that GitHub then re-states in `repositories_added` comes back tracked.
        GitHub sends genuinely-new repositories in that list, so this needs a
        provider quirk to reach; the cost of it is one delivery applied that an
        admin had declined, and the fix is one tick of a checkbox. The upgrade
        is to read the existing rows' `tracked` before the delete and carry it
        onto the insert -- a round trip and a widened `add_repositories`, which
        is not worth buying until somebody sees it happen.
        """
        added = _repositories(payload.get("repositories_added")) or ()
        removed = _repositories(payload.get("repositories_removed")) or ()

        stale = [repository.repository_id for repository in (*added, *removed)]

        if stale:
            await self._repository.delete_development(
                connection,
                scope=scope,
                repository_ids=stale,
            )
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
        automations: Sequence[GithubAutomationEntity] = (),
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
            automations=tuple(automations),
        )
