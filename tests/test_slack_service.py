"""The Slack service and its two seams. No database, no network, no secrets.

What is under test here is the part of the feature that handles a credential:
where the bot token goes, what comes back out, and what never travels with it.
The token store is a protocol with one implementation, so these tests
substitute a second one -- which is the only way to observe that the service
writes the store's *reference* rather than the token it was handed.
"""

from urllib.parse import parse_qs, urlparse

import asyncpg
import pytest

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.slack import (
    SLACK_ADMIN_ROLES,
    SlackGrant,
    SlackIntegrationView,
    SlackOAuthError,
)
from app.domain.tenancy import WORKSPACE_ROLES, AuthorizedWorkspaceScope
from app.repositories.slack import SlackRepository
from app.services.slack import (
    REQUESTED_SCOPES,
    SLACK_AUTHORIZE_URL,
    DatabaseTokenStore,
    SlackService,
    _grant_from_response,
    authorize_url,
)

from tests.conftest import FakeConnection, FakePool, normalize
from tests.test_slack_graphql import VIEWER_ID, WORKSPACE_A, installation_row


BOT_TOKEN = "xoxb-not-a-real-token"


def scope(role="admin"):
    return AuthorizedWorkspaceScope(
        workspace_id=WORKSPACE_A,
        user_id=VIEWER_ID,
        role=role,
    )


def grant():
    return SlackGrant(
        slack_team_id="T024BE7LD",
        slack_team_name="Vector HQ",
        bot_user_id="U0BOTBOT",
        scopes=("channels:read", "chat:write"),
        bot_token=BOT_TOKEN,
    )


class RecordingTokenStore:
    """A second store, so the seam is observable.

    With only DatabaseTokenStore in play the reference and the token are the
    same string, so nothing could tell whether the service wrote what the
    store returned or the token it started with. This one returns a value the
    token cannot be mistaken for.
    """

    backend = "vault"

    def __init__(self):
        self.stored: list[str] = []

    async def store(self, *, workspace_id, token):
        self.stored.append(token)

        return f"vault://{workspace_id}"

    async def fetch(self, *, workspace_id, reference):
        return BOT_TOKEN


def service(*, pool=None, store=None, configured=True):
    return SlackService(
        pool=pool if pool is not None else FakePool(),
        repository=SlackRepository(),
        token_store=store if store is not None else DatabaseTokenStore(),
        configured=configured,
    )


# --- the token store --------------------------------------------------


async def test_the_database_store_round_trips_and_names_itself():
    """The reference IS the token, and the backend tag says so.

    The tag is what makes replacing this store a swap rather than a data
    migration: a row written by one store records which one, so a KMS store
    can be introduced beside rows it did not write.
    """
    store = DatabaseTokenStore()

    reference = await store.store(workspace_id=WORKSPACE_A, token=BOT_TOKEN)

    assert store.backend == "database"
    assert reference == BOT_TOKEN
    assert await store.fetch(workspace_id=WORKSPACE_A, reference=reference) == BOT_TOKEN


async def test_connect_writes_the_stores_reference_and_never_the_raw_token():
    """The one assertion the whole abstraction exists for.

    The token reaches the store and stops there; what goes into the INSERT is
    the reference and the backend that issued it. With the database store
    those two strings happen to be equal, which is exactly why this test uses
    a different store -- otherwise the property is unobservable and could
    regress unnoticed.
    """
    store = RecordingTokenStore()
    connection = FakeConnection(row=installation_row())

    await service(pool=FakePool(connection), store=store).connect(
        scope=scope(),
        grant=grant(),
    )

    assert store.stored == [BOT_TOKEN]

    inserts = [
        call for call in connection.queries if "INSERT INTO" in call["query"].upper()
    ]

    assert len(inserts) == 1

    arguments = inserts[0]["args"]

    assert f"vault://{WORKSPACE_A}" in arguments
    assert "vault" in arguments
    assert BOT_TOKEN not in arguments


async def test_the_view_a_connect_returns_carries_no_token_field():
    """A grant goes in; a status, a name and a list of scopes come out.

    Asserted on the dataclass rather than on one instance, so a field added to
    the view later fails here rather than in whatever renders it.
    """
    view = await service(pool=FakePool(FakeConnection(row=installation_row()))).connect(
        scope=scope(),
        grant=grant(),
    )

    assert set(SlackIntegrationView.__slots__) == {"status", "team_name", "scopes"}
    assert view.status == "connected"


async def test_a_token_written_by_another_store_is_refused_rather_than_returned():
    """A KMS reference handed to the database store is not a token.

    Without this check the reference would be returned as if it were the
    secret and sent to Slack as an Authorization header -- a failure that
    looks like an expired token and is actually a store mismatch.
    """
    connection = FakeConnection(
        row={"bot_token_backend": "vault", "bot_token_reference": "vault://x"}
    )

    with pytest.raises(RuntimeError) as raised:
        await service(pool=FakePool(connection)).bot_token(scope=scope())

    assert "vault" in str(raised.value)


async def test_bot_token_answers_none_for_a_workspace_with_no_installation():
    assert await service(pool=FakePool(FakeConnection())).bot_token(scope=scope()) is (
        None
    )


# --- the admin rule ---------------------------------------------------


@pytest.mark.parametrize("role", sorted(SLACK_ADMIN_ROLES))
def test_an_admin_or_owner_passes_the_rule(role):
    SlackService.require_admin(scope(role=role))


@pytest.mark.parametrize(
    "role",
    sorted(set(WORKSPACE_ROLES) - SLACK_ADMIN_ROLES),
)
def test_every_other_role_is_refused_as_an_access_denial(role):
    """The same exception a non-member gets, which is what makes the two
    externally indistinguishable at the transports."""
    with pytest.raises(WorkspaceAccessDeniedError):
        SlackService.require_admin(scope(role=role))


@pytest.mark.parametrize(
    "method",
    ["status", "disconnect", "bot_token"],
)
async def test_no_read_or_write_happens_for_a_non_admin(method):
    """The refusal comes before the pool is touched.

    An ExplodingPool would prove the same thing; a FakePool that counts
    acquisitions also shows that nothing was read on the way to deciding.
    """
    pool = FakePool()

    with pytest.raises(WorkspaceAccessDeniedError):
        await getattr(service(pool=pool), method)(scope=scope(role="member"))

    assert pool.acquire_count == 0


# --- the deduplication claim ------------------------------------------


@pytest.mark.parametrize(
    ("returned", "expected"),
    [("Ev0001", True), (None, False)],
    ids=["first delivery", "redelivery"],
)
async def test_a_claim_reports_whether_this_delivery_won(returned, expected):
    """One statement decides it, and the RETURNING row is the answer.

    `None` back from the insert means the row was already there, which is what
    a retry sees. Without RETURNING the statement would succeed identically
    either way and the caller would need a second query -- reopening the race
    the single statement closes.
    """
    connection = FakeConnection(value=returned)

    assert (
        await service(pool=FakePool(connection)).claim_event(event_id="Ev0001")
        is expected
    )


async def test_the_claim_is_one_statement_with_a_conflict_clause():
    """Pinned as SQL, because the shape is the guarantee.

    A SELECT-then-INSERT would pass a behavioural test against a fake and
    still lose the race against a real retry, so this asserts the statement
    rather than only its result.
    """
    connection = FakeConnection(value="Ev0001")

    await service(pool=FakePool(connection)).claim_event(event_id="Ev0001")

    assert len(connection.queries) == 1

    statement = normalize(connection.queries[0]["query"])

    assert "INSERT INTO slack_event_deliveries" in statement
    assert "ON CONFLICT (event_id) DO NOTHING" in statement
    assert "RETURNING event_id" in statement
    assert connection.queries[0]["args"] == ("Ev0001",)


# --- the OAuth response reader ----------------------------------------


def slack_response(**overrides):
    payload = {
        "ok": True,
        "access_token": BOT_TOKEN,
        "bot_user_id": "U0BOTBOT",
        "scope": "channels:read,chat:write",
        "team": {"id": "T024BE7LD", "name": "Vector HQ"},
    }

    return payload | overrides


def test_a_good_response_becomes_a_grant_with_split_scopes():
    """The comma-joined string is split at the boundary, once."""
    parsed = _grant_from_response(slack_response())

    assert parsed.scopes == ("channels:read", "chat:write")
    assert parsed.bot_user_id == "U0BOTBOT"
    assert parsed.slack_team_name == "Vector HQ"


@pytest.mark.parametrize(
    "overrides",
    [
        {"ok": False},
        {"ok": "true"},
        {"access_token": None},
        {"access_token": ""},
        {"bot_user_id": ""},
        {"scope": ""},
        {"team": {}},
        {"team": None},
        {"team": {"id": "T1"}},
    ],
    ids=[
        "refused",
        "ok is a string",
        "no token",
        "empty token",
        "empty bot user id",
        "no scopes",
        "empty team",
        "team is null",
        "team without a name",
    ],
)
def test_an_incomplete_response_is_refused_whole(overrides):
    """Half a grant is worse than none.

    An installation built from a partial response cannot post, cannot be told
    apart from a working one, and can only be fixed by an admin who first has
    to work out that it is broken. The empty-bot-user-id case is the sharpest:
    it would silently disable loop prevention.
    """
    with pytest.raises(SlackOAuthError):
        _grant_from_response(slack_response(**overrides))


# --- the authorize URL ------------------------------------------------


def test_the_authorize_url_carries_the_client_id_scopes_and_state():
    url = authorize_url(client_id="123.456", state="abc-state")

    assert url.startswith(SLACK_AUTHORIZE_URL + "?")
    assert "client_id=123.456" in url
    assert "state=abc-state" in url

    # Parsed rather than string-matched, because urlencode escapes the colons
    # in a Slack scope and a literal assertion would be pinning the escaping
    # rather than the scopes.
    query = parse_qs(urlparse(url).query)

    assert query["scope"] == [",".join(REQUESTED_SCOPES)]


def test_a_state_cannot_smuggle_extra_parameters_into_the_authorize_url():
    """Everything is urlencoded, so a state is a value and not syntax."""
    url = authorize_url(client_id="123.456", state="a&redirect_uri=https://evil")

    assert "redirect_uri=https://evil" not in url
    assert "%26redirect_uri" in url


# --- the constraint translation ---------------------------------------


async def test_a_taken_slack_workspace_is_reported_as_an_expected_outcome():
    """The one unique violation this path expects, and only that one."""
    from app.domain.slack import SlackTeamAlreadyConnectedError

    error = asyncpg.UniqueViolationError("duplicate key")
    error.constraint_name = "slack_installations_team_key"

    assert isinstance(
        SlackService._already_connected(error), (SlackTeamAlreadyConnectedError)
    )


async def test_a_unique_violation_from_another_constraint_is_handed_back():
    """A constraint added by a later migration is not this error.

    Translating it anyway is how a schema change turns into a wrong message
    to an admin instead of a loud failure in a log.
    """
    error = asyncpg.UniqueViolationError("duplicate key")
    error.constraint_name = "some_future_constraint"

    assert SlackService._already_connected(error) is error
