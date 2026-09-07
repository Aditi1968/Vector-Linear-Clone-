"""Slack after the connect button: channels, a default, and preferences.

No database, no network, no credentials. What is under test is the part of the
feature that talks to a third party and then tells an admin what happened, so
the assertions cluster around three claims:

  * a channel id a client sent is never trusted -- it is resolved against the
    caller's OWN workspace, and the name that gets stored comes from that row;
  * nothing reports a success it did not observe. A sync that could not reach
    Slack says so and hands back the cache; a test notification is `delivered`
    only for a Slack response that said `ok: true`;
  * Slack's own error vocabulary stops at one function. Everything downstream
    of `_failure_for` speaks in this product's words.

The Web API is a protocol with one real implementation, so these tests
substitute it -- which is not only about avoiding the network: there are no
Slack credentials in this environment, so a test that reached the real client
could not be written at all.
"""

import json

import httpx
import pytest

from app.domain.errors import ValidationError
from app.domain.slack import (
    SLACK_ADMIN_ROLES,
    SLACK_FAILURES,
    SLACK_NOTIFICATION_EVENTS,
    SlackApiError,
    SlackChannelEntity,
)
from app.graphql.schema import build_schema
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.types.slack import SlackFailureType, SlackNotificationEventType
from app.repositories.slack import SlackRepository
from app.services.slack import (
    REQUESTED_SCOPES,
    SCOPE_LIST_CHANNELS,
    SCOPE_POST_MESSAGE,
    TEST_MESSAGE_TEXT,
    DatabaseTokenStore,
    SlackService,
    SlackWebClient,
    _channels_from_response,
    _failure_for,
    _web_call,
)

from tests.conftest import ExplodingPool, FakePool, normalize
from tests.test_slack_graphql import (
    SLUG_A,
    SLUG_B,
    VIEWER_ID,
    WORKSPACE_A,
    Context,
    FakeMembershipService,
    installation_row,
    scope,
)


schema = build_schema("test")

BOT_TOKEN = "xoxb-not-a-real-token"

CHANNELS_QUERY = """
query SlackChannels($slug: String!) {
  slackChannels(workspaceSlug: $slug) {
    id
    name
    isMember
    isAccessible
  }
}
"""

SETTINGS_QUERY = """
query SlackSettings($slug: String!) {
  slackNotificationSettings(workspaceSlug: $slug) {
    defaultChannelId
    defaultChannelName
    preferences { event enabled }
  }
}
"""

SYNC_MUTATION = """
mutation SlackSync($slug: String!) {
  slackChannelsSync(input: { workspaceSlug: $slug }) {
    channels { id name }
    failure
  }
}
"""

SET_CHANNEL_MUTATION = """
mutation SlackSetChannel($slug: String!, $channel: String!) {
  slackDefaultChannelSet(input: { workspaceSlug: $slug, channelId: $channel }) {
    settings { defaultChannelId defaultChannelName }
    errors { field code message }
  }
}
"""

SET_PREFERENCE_MUTATION = """
mutation SlackSetPreference($slug: String!) {
  slackNotificationPreferenceSet(
    input: { workspaceSlug: $slug, event: ISSUE_ASSIGNED, enabled: true }
  ) {
    settings { preferences { event enabled } }
    errors { field code }
  }
}
"""

TEST_NOTIFICATION_MUTATION = """
mutation SlackTest($slug: String!) {
  slackTestNotification(input: { workspaceSlug: $slug }) {
    delivered
    failure
  }
}
"""


def channel(
    channel_id="C0ENGINEER",
    name="engineering",
    *,
    is_private=False,
    is_archived=False,
    is_member=True,
    is_accessible=True,
):
    return SlackChannelEntity(
        channel_id=channel_id,
        name=name,
        is_private=is_private,
        is_archived=is_archived,
        is_member=is_member,
        is_accessible=is_accessible,
    )


def channel_row(entity: SlackChannelEntity) -> dict:
    """A row shaped like the repository's SELECT list, as a dict.

    Keyed by column name rather than by entity attribute, so a column renamed
    in the statement and not here fails the mapping the way a real row would.
    """
    return {
        "channel_id": entity.channel_id,
        "name": entity.name,
        "is_private": entity.is_private,
        "is_archived": entity.is_archived,
        "is_member": entity.is_member,
        "is_accessible": entity.is_accessible,
    }


class RoutingConnection:
    """A fake connection that answers by statement rather than with one row.

    `tests.conftest.FakeConnection` replays a single canned row for every
    query, which is enough for a repository method and not enough for a service
    method: `sync_channels` reads an installation, a token reference and the
    channel cache on one connection, and each wants a differently-shaped
    answer. Handing all three the same dict is how a test passes because
    `find_token_reference` happened to raise nothing.

    Dispatch is on the table named in the statement, so a query aimed at a
    table this fake was not told about fails loudly here rather than silently
    receiving somebody else's row.
    """

    def __init__(
        self,
        *,
        installation=None,
        token_reference=None,
        channels=(),
        default_channel=None,
        preferences=(),
    ):
        self.installation = installation
        self.token_reference = token_reference
        self.channels = list(channels)
        self.default_channel = default_channel
        self.preferences = list(preferences)
        self.queries: list[dict] = []

    def transaction(self):
        return _NullTransaction()

    async def execute(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return "INSERT 0 1"

    async def fetch(self, query, *args):
        self.queries.append({"query": query, "args": args})
        statement = normalize(query)

        if "FROM slack_channels" in statement:
            return [channel_row(entity) for entity in self.channels]

        if "FROM slack_notification_preferences" in statement:
            return [
                {"event": event, "enabled": enabled}
                for event, enabled in self.preferences
            ]

        raise AssertionError(f"this fake has no rows for: {statement}")

    async def fetchrow(self, query, *args):
        self.queries.append({"query": query, "args": args})
        statement = normalize(query)

        if "bot_token_backend" in statement:
            if self.token_reference is None:
                return None

            backend, reference = self.token_reference

            return {
                "bot_token_backend": backend,
                "bot_token_reference": reference,
            }

        if "FROM slack_installations" in statement:
            return self.installation

        if "FROM slack_channels" in statement:
            wanted = args[1]

            return next(
                (
                    channel_row(entity)
                    for entity in self.channels
                    if entity.channel_id == wanted
                ),
                None,
            )

        if "FROM slack_notification_settings" in statement:
            if self.default_channel is None:
                return None

            channel_id, name = self.default_channel

            return {
                "default_channel_id": channel_id,
                "default_channel_name": name,
            }

        raise AssertionError(f"this fake has no row for: {statement}")

    async def fetchval(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return None

    def statements(self, needle: str) -> list[dict]:
        return [
            call for call in self.queries if needle in normalize(call["query"]).upper()
        ]


class _NullTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeWeb:
    """A Slack Web API that answers from a script and records what it was asked.

    Records rather than merely answering, because half the claims in this file
    are about calls that must NOT happen: a sync without the granted scope, a
    test notification with no channel chosen. A fake that only returned values
    would let those pass while still calling Slack.
    """

    def __init__(self, *, channels=(), listing_error=None, post_error=None):
        self.channels = list(channels)
        self.listing_error = listing_error
        self.post_error = post_error
        self.listings: list[str] = []
        self.posts: list[dict] = []

    async def conversations_list(self, *, token):
        self.listings.append(token)

        if self.listing_error is not None:
            raise self.listing_error

        return list(self.channels)

    async def post_message(self, *, token, channel, text):
        self.posts.append({"token": token, "channel": channel, "text": text})

        if self.post_error is not None:
            raise self.post_error


class ExplodingWeb:
    """A Web API that fails the test if anything reaches Slack at all."""

    async def conversations_list(self, *, token):
        raise AssertionError("Slack must not be called on this path")

    async def post_message(self, *, token, channel, text):
        raise AssertionError("Slack must not be called on this path")


def service(*, connection=None, web=None, configured=True):
    """The REAL service over fakes.

    Not a fake service: the rules under test -- which refusals happen before a
    network call, what a failed sync answers with, what makes `delivered` true
    -- are this class's, and a fake would only assert that the test's copy of
    them agrees with itself.
    """
    return SlackService(
        pool=FakePool(connection if connection is not None else RoutingConnection()),
        repository=SlackRepository(),
        token_store=DatabaseTokenStore(),
        configured=configured,
        web=web,
    )


def connected(**overrides):
    """A connection to a workspace with a live installation and a token."""
    defaults = {
        "installation": installation_row(),
        "token_reference": ("database", BOT_TOKEN),
    }

    return RoutingConnection(**(defaults | overrides))


# --- the vocabularies, pinned -----------------------------------------


def test_the_event_enum_matches_the_domain_vocabulary():
    """Three copies of one list: this enum, SLACK_NOTIFICATION_EVENTS, and the
    CHECK in migration 018.

    Pinned rather than trusted, because a mismatch is silent in both
    directions. An event the domain has and the enum does not raises inside
    `from_entity` and reaches the client as a masked internal error; one the
    enum has and the database does not is a constraint violation on a toggle,
    which is a 500 on a settings screen.
    """
    assert tuple(member.value for member in SlackNotificationEventType) == (
        SLACK_NOTIFICATION_EVENTS
    )


def test_the_failure_enum_matches_the_domain_vocabulary():
    assert tuple(member.value for member in SlackFailureType) == SLACK_FAILURES


def test_no_slack_error_string_is_spelled_in_the_failure_enum():
    """The enum is this product's words, not Slack's.

    Slack's `error` identifiers change without notice and are not a documented
    stable set. If one ever reached this enum, a client's switch statement
    would be pinned to a third party's spelling -- and the day Slack renames it
    the UI silently falls through to its default branch.
    """
    published = {member.value for member in SlackFailureType}

    for slack_code in ("not_in_channel", "channel_not_found", "invalid_auth"):
        assert slack_code not in published


def test_the_feature_needs_no_scope_this_release_does_not_request():
    """The gate on silently widening the OAuth grant.

    Channel discovery reads `channels:read` and the test notification writes
    with `chat:write`, and both are already in REQUESTED_SCOPES. A feature that
    needed a third would have to add it here, which makes broadening the grant
    a visible diff rather than a line in a service nobody re-reads.

    Note what this does NOT claim: `chat:write` permits posting only to
    channels the bot has joined. Posting to any public channel would need
    `chat:write.public`, which is deliberately not requested -- see
    SlackService.send_test_notification.
    """
    assert SCOPE_LIST_CHANNELS in REQUESTED_SCOPES
    assert SCOPE_POST_MESSAGE in REQUESTED_SCOPES


# --- syncing the channel list -----------------------------------------


async def test_a_sync_stores_ids_and_marks_channels_slack_no_longer_lists():
    """The two statements that make this a replace rather than an append.

    The upsert conflicts on (workspace_id, channel_id) -- the id, never the
    name -- so a channel renamed in Slack updates its row instead of becoming a
    second one. The second statement then marks everything the listing did not
    mention, rather than deleting it: a deleted row would abort the sync on
    `slack_notification_settings_default_channel_fk` whenever the missing
    channel was the one an admin had chosen.
    """
    connection = connected()
    web = FakeWeb(channels=[channel(), channel("C0DESIGN", "design")])

    await service(connection=connection, web=web).sync_channels(scope=scope())

    inserts = connection.statements("INSERT INTO SLACK_CHANNELS")

    assert len(inserts) == 1

    statement = normalize(inserts[0]["query"])

    assert "ON CONFLICT (workspace_id, channel_id) DO UPDATE" in statement
    assert inserts[0]["args"][1] == ["C0ENGINEER", "C0DESIGN"]

    marked = connection.statements("SET IS_ACCESSIBLE = FALSE")

    assert len(marked) == 1
    assert marked[0]["args"] == (WORKSPACE_A, ["C0ENGINEER", "C0DESIGN"])


async def test_a_sync_refreshes_the_denormalised_name_of_a_renamed_channel():
    """The third statement, and the reason the duplicate name is safe to hold.

    `slack_notification_settings.default_channel_name` exists so a settings
    screen can name a channel it can no longer reach. That is only worth having
    if something keeps it in step, and this is the only thing that does.
    """
    connection = connected()

    await service(connection=connection, web=FakeWeb()).sync_channels(scope=scope())

    refreshes = connection.statements("UPDATE SLACK_NOTIFICATION_SETTINGS")

    assert len(refreshes) == 1

    statement = normalize(refreshes[0]["query"])

    assert "default_channel_name = channel.name" in statement
    # Joined through the settings row's OWN workspace_id, so the name can only
    # ever come from a channel in the same tenant.
    assert "channel.workspace_id = settings.workspace_id" in statement


async def test_a_sync_without_the_granted_scope_never_reaches_slack():
    """The granted list decides, not what this release asks for.

    An admin can decline scopes individually, so `REQUESTED_SCOPES` says
    nothing about a particular workspace. Calling anyway would spend a round
    trip to be told `missing_scope` -- and would report Slack's refusal as a
    general failure rather than as the one thing an admin can fix by
    reconnecting.
    """
    connection = connected(installation=installation_row() | {"scopes": ["chat:write"]})

    sync = await service(connection=connection, web=ExplodingWeb()).sync_channels(
        scope=scope()
    )

    assert sync.failure == "missing_scope"


async def test_a_sync_for_a_workspace_with_no_installation_never_reaches_slack():
    sync = await service(
        connection=RoutingConnection(),
        web=ExplodingWeb(),
    ).sync_channels(scope=scope())

    assert sync.failure == "not_connected"
    assert sync.channels == ()


async def test_a_failed_sync_answers_with_the_cache_and_writes_nothing():
    """A picker that blanked itself whenever Slack was slow would look like a
    workspace that had lost its channels.

    Two halves, and the second is the one a behavioural test would miss: the
    cached channels come back AND no write happened, so a later successful sync
    is what changes the stored list rather than this one having half-cleared
    it.
    """
    connection = connected(channels=[channel()])
    web = FakeWeb(listing_error=SlackApiError())

    sync = await service(connection=connection, web=web).sync_channels(scope=scope())

    assert sync.failure == "slack_unreachable"
    assert [entity.channel_id for entity in sync.channels] == ["C0ENGINEER"]
    assert connection.statements("INSERT INTO SLACK_CHANNELS") == []


async def test_no_pool_connection_is_held_while_slack_is_called():
    """Two acquisitions, not one, and that is the whole assertion.

    A single `async with pool.acquire()` around the network call would hold a
    connection for as long as Slack takes to answer. A handful of admins
    pressing refresh while Slack is degraded would then exhaust the pool -- an
    outage in issue creation caused by a settings page.
    """
    pool = FakePool(connected())
    slack = SlackService(
        pool=pool,
        repository=SlackRepository(),
        token_store=DatabaseTokenStore(),
        configured=True,
        web=FakeWeb(channels=[channel()]),
    )

    await slack.sync_channels(scope=scope())

    assert pool.acquire_count == 2


async def test_a_service_built_without_a_web_client_fails_loudly():
    """A composition mistake, reported as one.

    Answering SLACK_UNREACHABLE would send an operator to check Slack's status
    page for a missing constructor argument.
    """
    with pytest.raises(RuntimeError) as raised:
        await service(connection=connected(), web=None).sync_channels(scope=scope())

    assert "web=" in str(raised.value)


# --- choosing a default channel ---------------------------------------


async def test_a_channel_id_from_another_workspace_answers_as_a_typo():
    """The attack this feature's lookup exists for.

    An admin of workspace A submits workspace B's channel id. The lookup is
    scoped to A's own workspace, so it finds nothing -- and the answer is the
    same field error a misspelled id gets. Anything that distinguished them
    would confirm, to anyone who can reach a settings page, that a particular
    Slack channel is connected to this deployment.
    """
    connection = connected(channels=[channel()])

    with pytest.raises(ValidationError) as raised:
        await service(connection=connection).set_default_channel(
            scope=scope(),
            channel_id="C0SOMEONEELSE",
        )

    assert [issue.code for issue in raised.value.issues] == ["unknown_channel"]
    assert connection.statements("INSERT INTO SLACK_NOTIFICATION_SETTINGS") == []


async def test_a_channel_the_last_sync_could_not_see_is_refused():
    connection = connected(channels=[channel(is_accessible=False)])

    with pytest.raises(ValidationError) as raised:
        await service(connection=connection).set_default_channel(
            scope=scope(),
            channel_id="C0ENGINEER",
        )

    assert [issue.code for issue in raised.value.issues] == ["unknown_channel"]


async def test_an_archived_channel_is_refused_while_the_admin_is_choosing():
    """Storable and useless, which is the worst combination.

    Posting to an archived channel fails, and the moment to say so is while
    somebody is picking -- not the first time an issue is assigned and nothing
    appears.
    """
    connection = connected(channels=[channel(is_archived=True)])

    with pytest.raises(ValidationError) as raised:
        await service(connection=connection).set_default_channel(
            scope=scope(),
            channel_id="C0ENGINEER",
        )

    assert [issue.code for issue in raised.value.issues] == ["channel_archived"]


async def test_the_stored_channel_name_comes_from_the_row_not_the_request():
    """The denormalised name has to start out true.

    The composite foreign key in 018 refuses a channel from another tenant, but
    a NAME is not part of that key -- nothing in the schema could catch a
    client-supplied one, so the only defence is that the service never reads a
    name off the request. There is no field for one on the input type either;
    this asserts the half that a future field could break.
    """
    connection = connected(channels=[channel(name="engineering")])

    settings = await service(connection=connection).set_default_channel(
        scope=scope(),
        channel_id="C0ENGINEER",
    )

    inserts = connection.statements("INSERT INTO SLACK_NOTIFICATION_SETTINGS")

    assert len(inserts) == 1
    assert inserts[0]["args"] == (WORKSPACE_A, "C0ENGINEER", "engineering")
    assert settings.default_channel_name == "engineering"


async def test_choosing_a_channel_before_connecting_is_a_field_error():
    """Not a foreign-key violation.

    Every table in 018 hangs off `slack_installations`, so without this check
    the write fails on a constraint -- which reaches the client as "Internal
    server error" and the log as a traceback for something that is not a
    defect.
    """
    with pytest.raises(ValidationError) as raised:
        await service(connection=RoutingConnection()).set_default_channel(
            scope=scope(),
            channel_id="C0ENGINEER",
        )

    assert [issue.code for issue in raised.value.issues] == ["not_connected"]


# --- notification preferences -----------------------------------------


async def test_every_event_is_reported_even_with_no_rows_stored():
    """A settings screen rendered from the stored rows alone would have no
    toggles at all on the day Slack is connected."""
    settings = await service(connection=connected()).notification_settings(
        scope=scope()
    )

    assert [preference.event for preference in settings.preferences] == list(
        SLACK_NOTIFICATION_EVENTS
    )
    assert all(not preference.enabled for preference in settings.preferences)


async def test_a_stored_row_wins_over_the_default():
    connection = connected(preferences=[("issue_completed", True)])

    settings = await service(connection=connection).notification_settings(scope=scope())

    enabled = {
        preference.event for preference in settings.preferences if preference.enabled
    }

    assert enabled == {"issue_completed"}


async def test_a_row_for_an_event_this_release_retired_is_not_surfaced():
    """What makes retiring an event a code change rather than a data migration.

    The row can stay in the table until somebody deletes it, and no client is
    handed a value its enum cannot name -- which would otherwise raise inside
    `from_entity` and be masked as an internal error.
    """
    connection = connected(preferences=[("issue_shouted_about", True)])

    settings = await service(connection=connection).notification_settings(scope=scope())

    assert [preference.event for preference in settings.preferences] == list(
        SLACK_NOTIFICATION_EVENTS
    )


async def test_turning_a_preference_off_is_stored_rather_than_deleted():
    """ "Never configured" and "deliberately disabled" are different facts.

    The first is a default a later release may change; the second is a decision
    it must not. A toggle implemented as insert-or-delete cannot tell them
    apart.
    """
    connection = connected()

    await service(connection=connection).set_notification_preference(
        scope=scope(),
        event="issue_assigned",
        enabled=False,
    )

    writes = connection.statements("INSERT INTO SLACK_NOTIFICATION_PREFERENCES")

    assert len(writes) == 1
    assert writes[0]["args"] == (WORKSPACE_A, "issue_assigned", False)
    assert connection.statements("DELETE FROM") == []


async def test_an_event_outside_the_vocabulary_is_refused_by_the_service():
    """The GraphQL enum already makes this unrepresentable over that transport.

    The rule still lives here, because the enum guards one transport and the
    database's CHECK would answer with a constraint violation -- a 500 -- for
    anything that reached it another way.
    """
    with pytest.raises(ValidationError) as raised:
        await service(connection=connected()).set_notification_preference(
            scope=scope(),
            event="issue_shouted_about",
            enabled=True,
        )

    assert [issue.code for issue in raised.value.issues] == ["unknown_event"]


# --- the test notification --------------------------------------------


async def test_a_test_notification_reports_delivered_only_when_slack_said_so():
    connection = connected(default_channel=("C0ENGINEER", "engineering"))
    web = FakeWeb()

    result = await service(connection=connection, web=web).send_test_notification(
        scope=scope()
    )

    assert result.delivered is True
    assert result.failure is None
    assert web.posts == [
        {"token": BOT_TOKEN, "channel": "C0ENGINEER", "text": TEST_MESSAGE_TEXT}
    ]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("not_in_channel", "channel_unavailable"),
        ("channel_not_found", "channel_unavailable"),
        ("is_archived", "channel_unavailable"),
        ("missing_scope", "missing_scope"),
        ("invalid_auth", "not_connected"),
        ("token_revoked", "not_connected"),
        ("ratelimited", "slack_refused"),
        (None, "slack_unreachable"),
    ],
)
async def test_a_refused_post_is_never_reported_as_delivered(code, expected):
    """The claim the whole button exists to make.

    An admin presses this because they do not yet believe the integration
    works, so a false positive here is worse than no button. Every refusal
    Slack can answer with -- and the case where Slack answers nothing at all --
    comes back `delivered=False`, with a reason in this product's vocabulary.

    `not_in_channel` is the commonest one in practice: `chat:write` permits
    posting to channels the bot has joined, and a public channel it was never
    invited to refuses. It maps to CHANNEL_UNAVAILABLE, which is what lets the
    product say "invite Vector to #engineering".
    """
    connection = connected(default_channel=("C0ENGINEER", "engineering"))
    web = FakeWeb(post_error=SlackApiError(code))

    result = await service(connection=connection, web=web).send_test_notification(
        scope=scope()
    )

    assert result.delivered is False
    assert result.failure == expected


async def test_a_test_notification_with_no_channel_chosen_never_calls_slack():
    result = await service(
        connection=connected(),
        web=ExplodingWeb(),
    ).send_test_notification(scope=scope())

    assert result.delivered is False
    assert result.failure == "no_default_channel"


async def test_a_test_notification_without_chat_write_never_calls_slack():
    connection = connected(
        installation=installation_row() | {"scopes": ["channels:read"]},
        default_channel=("C0ENGINEER", "engineering"),
    )

    result = await service(
        connection=connection,
        web=ExplodingWeb(),
    ).send_test_notification(scope=scope())

    assert result.failure == "missing_scope"


# --- reading Slack's answers ------------------------------------------


def test_a_channel_missing_its_name_is_dropped_rather_than_failing_the_page():
    """The opposite of what `_grant_from_response` does, on purpose.

    A half-read OAuth grant becomes an installation that cannot post and cannot
    be told from a working one, so it is refused whole. A channel missing its
    name is one row of a picker: dropping it costs an admin one entry, while
    refusing the page costs them the whole feature because one channel in their
    workspace has a shape this version did not expect.
    """
    read = _channels_from_response(
        {
            "channels": [
                {"id": "C0A", "name": "engineering", "is_member": True},
                {"id": "C0B"},
                {"name": "orphan"},
                "not a channel at all",
                {"id": "", "name": "empty id"},
            ]
        }
    )

    assert [entity.channel_id for entity in read] == ["C0A"]
    assert read[0].is_member is True


def test_a_channel_name_longer_than_slack_allows_is_truncated_not_fatal():
    """`slack_channels_name_length` caps at 80, matching Slack's own ceiling.

    A longer name is a response this version does not understand -- and left
    alone it would abort the entire sync on a CHECK violation, which is a 500
    for one odd channel.
    """
    read = _channels_from_response({"channels": [{"id": "C0A", "name": "x" * 200}]})

    assert len(read[0].name) == 80


def test_flags_absent_from_a_channel_payload_read_as_false():
    """`is_member` is the one that matters: absent must not read as "the bot is
    in this channel", or a picker would offer a channel that cannot be posted
    to and give an admin no reason why."""
    read = _channels_from_response({"channels": [{"id": "C0A", "name": "general"}]})

    assert read[0].is_member is False
    assert read[0].is_private is False
    assert read[0].is_archived is False


@pytest.mark.parametrize(
    "payload",
    [{}, {"channels": None}, {"channels": "engineering"}],
    ids=["no key", "null", "not a list"],
)
def test_a_response_with_no_channel_list_reads_as_no_channels(payload):
    assert _channels_from_response(payload) == []


async def test_a_slack_refusal_carries_its_code_no_further_than_one_function():
    """Slack answers HTTP 200 with `{"ok": false}` for a refusal.

    The status code says nothing, so `ok` is the only signal -- and the code it
    carries reaches exactly one place, `_failure_for`, which turns it into this
    product's vocabulary.
    """
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"ok": False, "error": "not_in_channel"}
        )
    )

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SlackApiError) as raised:
            await _web_call(client, "POST", "https://slack.test/x", token=BOT_TOKEN)

    assert raised.value.code == "not_in_channel"
    assert _failure_for(raised.value) == "channel_unavailable"


async def test_the_bot_token_is_sent_as_a_bearer_and_never_raised():
    """The header is the only place the token appears.

    A raised exception travels into logs and error reporters, so the assertion
    is not only that the call was authenticated but that nothing about the
    failure carries the credential.
    """
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))

        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SlackApiError) as raised:
            await _web_call(client, "GET", "https://slack.test/x", token=BOT_TOKEN)

    assert seen == [f"Bearer {BOT_TOKEN}"]
    assert BOT_TOKEN not in repr(raised.value)
    # A 5xx is Slack being unwell, not Slack disagreeing: no code to report.
    assert raised.value.code is None
    assert _failure_for(raised.value) == "slack_unreachable"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=["a", "list"]),
        httpx.Response(200, json={"ok": "true"}),
        httpx.Response(429, json={"ok": False, "error": "ratelimited"}),
    ],
    ids=["not json", "not an object", "ok is a string", "rate limited"],
)
async def test_an_answer_this_version_cannot_read_is_never_a_success(response):
    """ "ok is a string" is the sharp one: truthy, and not an acknowledgement.

    Reading it as one would report a delivery that did not happen, which is the
    single failure this whole path is built to rule out.
    """
    transport = httpx.MockTransport(lambda request: response)

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(SlackApiError):
            await _web_call(client, "GET", "https://slack.test/x", token=BOT_TOKEN)


async def test_a_transport_failure_is_reported_as_unreachable_not_refused():
    """ "Slack said no" and "we never found out" are different sentences.

    Collapsing them would have the product tell an admin their channel is wrong
    when the truth is that Slack is down.
    """

    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SlackApiError) as raised:
            await _web_call(client, "GET", "https://slack.test/x", token=BOT_TOKEN)

    assert _failure_for(raised.value) == "slack_unreachable"


def mock_httpx(monkeypatch, handler):
    """Point every AsyncClient this module builds at `handler`.

    `SlackWebClient` opens its own client per call -- deliberately, so that the
    token is a local rather than instance state -- which leaves no seam to
    inject a transport through. Patching the module `httpx` is the honest way
    in: the real request-building, header-setting, paging and JSON reading all
    still run, and only the socket is replaced.
    """
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(**kwargs, transport=transport),
    )


async def test_the_listing_asks_slack_only_for_what_the_grant_covers(monkeypatch):
    """`types=public_channel`, because `channels:read` is what is granted.

    Adding `private_channel` would turn the whole call into `missing_scope` --
    so a request for data we may NOT read would take with it the data we may.

    `exclude_archived=false` is the other half, and it is not laziness: an
    archived channel may be the one an admin already chose, and hiding it from
    the sync would mark it inaccessible and leave the settings screen unable to
    explain why the notifications stopped.
    """
    asked: list[dict] = []

    def handler(request):
        asked.append(dict(request.url.params))

        return httpx.Response(200, json={"ok": True, "channels": []})

    mock_httpx(monkeypatch, handler)

    await SlackWebClient().conversations_list(token=BOT_TOKEN)

    assert asked == [
        {"types": "public_channel", "exclude_archived": "false", "limit": "200"}
    ]


async def test_the_listing_follows_slacks_cursor_and_stops_when_it_empties(monkeypatch):
    """A workspace with more channels than one page is the ordinary case.

    Slack signals the end with an EMPTY next_cursor rather than by omitting the
    key, so a loop testing for the key's presence would page forever against a
    cooperative server -- and the bound in MAX_CHANNEL_PAGES would be the only
    thing stopping it, silently truncating every large workspace's list.
    """
    pages = [
        {
            "ok": True,
            "channels": [{"id": "C0A", "name": "one"}],
            "response_metadata": {"next_cursor": "dXNlcjpV"},
        },
        {
            "ok": True,
            "channels": [{"id": "C0B", "name": "two"}],
            "response_metadata": {"next_cursor": ""},
        },
    ]
    cursors: list[str | None] = []

    def handler(request):
        cursors.append(request.url.params.get("cursor"))

        return httpx.Response(200, json=pages[len(cursors) - 1])

    mock_httpx(monkeypatch, handler)

    listed = await SlackWebClient().conversations_list(token=BOT_TOKEN)

    assert [entity.channel_id for entity in listed] == ["C0A", "C0B"]
    assert cursors == [None, "dXNlcjpV"]


async def test_a_post_sends_the_channel_id_and_the_fixed_text(monkeypatch):
    """The id, never the name. A name resolved at post time is how a message
    reaches the wrong room after somebody renames a channel."""
    bodies: list[dict] = []

    def handler(request):
        # Decoded rather than byte-matched, so the assertion is about what was
        # sent and not about how the encoder spaces its separators.
        bodies.append(json.loads(request.content))

        return httpx.Response(200, json={"ok": True})

    mock_httpx(monkeypatch, handler)

    await SlackWebClient().post_message(
        token=BOT_TOKEN,
        channel="C0ENGINEER",
        text=TEST_MESSAGE_TEXT,
    )

    assert bodies == [{"channel": "C0ENGINEER", "text": TEST_MESSAGE_TEXT}]


# --- the GraphQL surface ----------------------------------------------


def context(*, membership=None, slack=None, viewer_user_id=VIEWER_ID):
    return Context(
        membership_service=(
            membership if membership is not None else FakeMembershipService()
        ),
        slack_service=slack if slack is not None else service(connection=connected()),
        viewer_user_id=viewer_user_id,
    )


DOCUMENTS = [
    CHANNELS_QUERY,
    SETTINGS_QUERY,
    SYNC_MUTATION,
    SET_CHANNEL_MUTATION,
    SET_PREFERENCE_MUTATION,
    TEST_NOTIFICATION_MUTATION,
]

DOCUMENT_IDS = [
    "slackChannels",
    "slackNotificationSettings",
    "slackChannelsSync",
    "slackDefaultChannelSet",
    "slackNotificationPreferenceSet",
    "slackTestNotification",
]


async def run(document, *, slug=SLUG_A, **kwargs):
    return await schema.execute(
        document,
        variable_values={"slug": slug, "channel": "C0ENGINEER"},
        context_value=context(**kwargs),
    )


@pytest.mark.parametrize("document", DOCUMENTS, ids=DOCUMENT_IDS)
async def test_a_member_of_one_workspace_cannot_reach_anothers_slack_settings(
    document,
):
    """A NOT_FOUND that says nothing about whether workspace B exists.

    The exploding pool and the exploding Web API are the second half: no Slack
    table is read and no Slack call is made on behalf of a caller who was
    refused.
    """
    slack = SlackService(
        pool=ExplodingPool(),
        repository=SlackRepository(),
        token_store=DatabaseTokenStore(),
        configured=True,
        web=ExplodingWeb(),
    )

    result = await schema.execute(
        document,
        variable_values={"slug": SLUG_B, "channel": "C0ENGINEER"},
        context_value=context(
            membership=FakeMembershipService(slug=SLUG_A), slack=slack
        ),
    )

    assert result.errors is not None
    assert result.errors[0].message == WORKSPACE_NOT_FOUND_MESSAGE
    assert result.errors[0].extensions["code"] == "NOT_FOUND"


@pytest.mark.parametrize("document", DOCUMENTS, ids=DOCUMENT_IDS)
async def test_a_plain_member_is_refused_exactly_as_a_stranger_is(document):
    """To a member who may not administer the workspace, this does not exist.

    A distinct "forbidden" would tell every member of every workspace that this
    deployment has a Slack app, that this workspace is connected to it, and
    that the road to it is being an admin.
    """
    slack = SlackService(
        pool=ExplodingPool(),
        repository=SlackRepository(),
        token_store=DatabaseTokenStore(),
        configured=True,
        web=ExplodingWeb(),
    )

    result = await run(
        document,
        membership=FakeMembershipService(resolved=scope(role="member")),
        slack=slack,
    )

    assert result.errors is not None
    assert result.errors[0].message == WORKSPACE_NOT_FOUND_MESSAGE


@pytest.mark.parametrize("document", DOCUMENTS, ids=DOCUMENT_IDS)
async def test_an_unauthenticated_request_is_refused_before_any_lookup(document):
    slack = SlackService(
        pool=ExplodingPool(),
        repository=SlackRepository(),
        token_store=DatabaseTokenStore(),
        configured=True,
        web=ExplodingWeb(),
    )

    result = await run(document, slack=slack, viewer_user_id=None)

    assert result.errors is not None
    assert result.errors[0].extensions["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("role", sorted(SLACK_ADMIN_ROLES))
async def test_both_admin_roles_may_read_the_channel_list(role):
    """The control for the refusals above.

    Without it, a rule that refused everyone would pass every negative test in
    this file and ship a settings screen nobody can use.
    """
    result = await run(
        CHANNELS_QUERY,
        membership=FakeMembershipService(resolved=scope(role=role)),
        slack=service(connection=connected(channels=[channel()])),
    )

    assert result.errors is None
    assert result.data["slackChannels"] == [
        {
            "id": "C0ENGINEER",
            "name": "engineering",
            "isMember": True,
            "isAccessible": True,
        }
    ]


async def test_a_failed_sync_is_a_payload_field_and_not_a_masked_error():
    """Slack being unreachable is an ordinary state of a third-party
    integration, not a defect.

    Raised as a GraphQL error it would be masked into "Internal server error"
    -- which tells an admin nothing and tells them to file a bug.
    """
    slack = service(
        connection=connected(channels=[channel()]),
        web=FakeWeb(listing_error=SlackApiError()),
    )

    result = await run(SYNC_MUTATION, slack=slack)

    assert result.errors is None
    assert result.data["slackChannelsSync"]["failure"] == "SLACK_UNREACHABLE"
    assert result.data["slackChannelsSync"]["channels"] == [
        {"id": "C0ENGINEER", "name": "engineering"}
    ]


async def test_an_unknown_channel_is_a_field_error_not_a_masked_one():
    slack = service(connection=connected(channels=[channel()]))

    result = await schema.execute(
        SET_CHANNEL_MUTATION,
        variable_values={"slug": SLUG_A, "channel": "C0SOMEONEELSE"},
        context_value=context(slack=slack),
    )

    assert result.errors is None

    payload = result.data["slackDefaultChannelSet"]

    assert payload["settings"] is None
    assert payload["errors"][0]["field"] == "channelId"
    assert payload["errors"][0]["code"] == "unknown_channel"


async def test_a_test_notification_reports_its_outcome_through_the_payload():
    slack = service(
        connection=connected(default_channel=("C0ENGINEER", "engineering")),
        web=FakeWeb(post_error=SlackApiError("not_in_channel")),
    )

    result = await run(TEST_NOTIFICATION_MUTATION, slack=slack)

    assert result.errors is None
    assert result.data["slackTestNotification"] == {
        "delivered": False,
        "failure": "CHANNEL_UNAVAILABLE",
    }


def test_the_test_message_text_is_not_something_a_client_can_choose():
    """An authenticated way to make Vector say arbitrary words in a company's
    Slack would be exactly that, whatever it was called.

    Asserted against the built schema rather than by reading the input class,
    so a field added anywhere -- a subclass, a merge -- fails here.
    """
    fields = schema.as_str().split("input SlackTestNotificationInput {", 1)[1]
    fields = fields.split("}", 1)[0]

    assert fields.split() == ["workspaceSlug:", "String!"]


@pytest.mark.parametrize(
    "field",
    ["botToken", "token", "botTokenReference"],
)
def test_no_credential_field_arrived_on_the_channel_type(field):
    """The channel type is the newest place a secret could land.

    A validation error is the pass condition: the schema does not know the
    field at all.
    """
    document = CHANNELS_QUERY.replace("isMember", field)

    result = schema.execute_sync(document, variable_values={"slug": SLUG_A})

    assert result.errors is not None
    assert f"Cannot query field '{field}'" in result.errors[0].message


def test_the_channel_type_exposes_no_tenant_identifier():
    """A Slack team id or a Vector workspace id on this type would be an
    identifier handed to every browser that renders a picker, for no field a
    client needs."""
    block = schema.as_str().split("type SlackChannel {", 1)[1].split("}", 1)[0].lower()

    for forbidden in ("workspaceid", "teamid", "slackteamid"):
        assert forbidden not in block


def test_the_workspace_id_is_never_read_from_the_document():
    """Every Slack input names a workspace by SLUG and resolves it through a
    membership row.

    A `workspaceId` argument would be a way to name a tenant the caller was
    never checked against -- the shape CLAUDE.md refuses in one line: never
    trust workspace IDs supplied by the frontend.
    """
    sdl = schema.as_str()

    for block in sdl.split("input Slack")[1:]:
        assert "workspaceId" not in block.split("}", 1)[0]
