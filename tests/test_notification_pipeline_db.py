"""The whole pipeline, end to end, against a real schema and the real services.

`tests/test_migration_027_db.py` asks what the schema refuses.
`tests/test_notification_pipeline.py` asks what the import graph allows. This
asks whether an event that really happened turns into exactly the right number
of messages in exactly the right channel -- which is the only question a person
whose Slack this posts into actually has.

Every claim below is driven through the code that runs in production: a signed
GitHub delivery goes through `GithubService.apply_webhook`, an assignment goes
through `app.services.activity.record_changes`, a project update goes through
`ProjectService.post_update`, and every message goes out through
`SlackNotifier`. The only substitution is the Web API seam, because there are
no Slack credentials in this environment -- and because half the claims here
are about calls that must NOT happen, which a fake that merely returned values
could not catch.

The pool is deliberately `max_size=1`. That is not economy: it makes "a
connection is never held across the network call" a property this whole file
rests on, and `test_the_delivery_holds_no_connection_while_it_talks_to_slack`
is the assertion that turns it into a proof -- a notifier that held its
connection would deadlock against a pool with nothing left to give.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import asyncio
from datetime import timedelta
from uuid import UUID

import asyncpg
import pytest

from app.domain.activity import IssueSnapshot
from app.domain.slack import SlackApiError
from app.domain.tenancy import WorkspaceScope
from app.repositories.events import EventRepository
from app.repositories.github import GithubRepository
from app.repositories.initiatives import InitiativeRepository
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository
from app.repositories.slack import SlackRepository
from app.services.activity import record_changes
from app.services.github import GithubAppConfig, GithubService
from app.services.notifications import MAX_ATTEMPTS, SlackNotifier
from app.services.projects import ProjectService
from app.services.slack import DatabaseTokenStore

from tests.conftest import (
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

# The tenant 002 seeds, and its team, whose key 005 assigns as CORE.
WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")
TEAM_KEY = "CORE"

# The slug 002 seeds it with, and half of every deep link a message carries.
WORKSPACE_SLUG = "vector"

# The other tenant, real in every respect -- team, member, Slack installation,
# channel, preferences, issue -- so the cross-tenant assertion fails for the
# reason it names rather than because the target was missing.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
OTHER_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b2")
PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c1")

ISSUE_NUMBER = 142

INSTALLATION_ID = 4242
REPOSITORY_ID = 11111
PULL_NUMBER = 84

CHANNEL_ID = "C0ENGINEERING"
OTHER_CHANNEL_ID = "C0THEIRBOARD"

# Placeholders, not credentials. Distinct per workspace precisely so that
# `test_one_workspaces_event_never_reaches_another_workspaces_channel` can
# assert the message went out under the right one -- a channel check alone
# would pass for a delivery that presented the wrong tenant's bot.
TOKEN_REFERENCE = "placeholder-ours"
OTHER_TOKEN_REFERENCE = "placeholder-theirs"

BASE_URL = "https://app.vector.test"

MERGED_AT = "2026-04-01T10:00:00Z"

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

INSERT_ISSUE_SQL = """
INSERT INTO issues (
    id, workspace_id, team_id, number, workflow_state_id, title, priority
)
VALUES (
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
    ),
    $5, 3
)
"""

INSERT_INSTALLATION_SQL = """
INSERT INTO slack_installations (
    workspace_id,
    slack_team_id,
    slack_team_name,
    bot_user_id,
    scopes,
    bot_token_backend,
    bot_token_reference,
    connected_by_user_id
)
VALUES (
    $1, $2, $3, 'U0BOTBOT',
    ARRAY['channels:read', 'chat:write'],
    'database', $4, $5
)
"""


class FakeWeb:
    """A Slack Web API that answers from a script and records what it was asked.

    Records rather than merely answering, because most of the claims in this
    file are about calls that must not happen: a disabled preference, a
    workspace with no channel, a redelivery. A fake that only returned values
    would let every one of those pass while still posting.
    """

    def __init__(self, *, post_error: Exception | None = None, pool=None):
        self.post_error = post_error

        # When set, every post reaches into the pool -- which is what turns
        # "no connection is held across the network call" into an assertion
        # rather than a comment. See the test that uses it.
        self.pool = pool

        self.posts: list[dict] = []

    async def conversations_list(self, *, token):
        raise AssertionError("the delivery path must never list channels")

    async def post_message(self, *, token, channel, text):
        if self.pool is not None:
            async with self.pool.acquire() as connection:
                await connection.fetchval("SELECT 1")

        self.posts.append({"token": token, "channel": channel, "text": text})

        if self.post_error is not None:
            raise self.post_error


@pytest.fixture
async def pool(postgres_dsn):
    """A pool of ONE over a fully migrated database with two populated tenants.

    One connection, deliberately. Every delivery in this file therefore runs
    with the pool exhausted the instant anything holds a connection, so a
    notifier that kept one across the Slack call could not complete a single
    test here.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)
        await seed(connection)
    finally:
        await connection.close()

    created = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=1)

    try:
        yield created
    finally:
        await created.close()

        cleanup = await asyncpg.connect(postgres_dsn)

        try:
            await reset_schema(cleanup)
        finally:
            await cleanup.close()


async def seed(connection) -> None:
    """Two workspaces, each connected to Slack with a channel of its own.

    Symmetric on purpose. The cross-tenant assertion is "workspace A's event,
    workspace B's channel", and a lopsided fixture -- where one side has no
    channel to reach for -- would pass it for the wrong reason.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        OTHER_WORKSPACE_ID,
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, 'Other', 'OTH')",
        OTHER_TEAM_ID,
        OTHER_WORKSPACE_ID,
    )
    await seed_workflow_states(connection, OTHER_WORKSPACE_ID, OTHER_TEAM_ID)

    for user_id in (MEMBER_ID, OUTSIDER_ID):
        await connection.execute(INSERT_USER_SQL, user_id)

    for workspace_id, user_id, team, reference, channel_id, name in (
        (WORKSPACE_ID, MEMBER_ID, "T0OURS", TOKEN_REFERENCE, CHANNEL_ID, "eng"),
        (
            OTHER_WORKSPACE_ID,
            OUTSIDER_ID,
            "T0THEIRS",
            OTHER_TOKEN_REFERENCE,
            OTHER_CHANNEL_ID,
            "board",
        ),
    ):
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, 'admin')",
            workspace_id,
            user_id,
        )
        await connection.execute(
            INSERT_INSTALLATION_SQL, workspace_id, team, team, reference, user_id
        )
        await connection.execute(
            "INSERT INTO slack_channels (workspace_id, channel_id, name) "
            "VALUES ($1, $2, $3)",
            workspace_id,
            channel_id,
            name,
        )
        await connection.execute(
            "INSERT INTO slack_notification_settings "
            "(workspace_id, default_channel_id, default_channel_name) "
            "VALUES ($1, $2, $3)",
            workspace_id,
            channel_id,
            name,
        )

    # The GitHub side, for the merge. Confirmed on the way in: an unconfirmed
    # claim routes no delivery, and 016 refuses an account login on one anyway.
    await connection.execute(
        "INSERT INTO github_installations "
        "(workspace_id, installation_id, connected_by, confirmed_at) "
        "VALUES ($1, $2, $3, now())",
        WORKSPACE_ID,
        INSTALLATION_ID,
        MEMBER_ID,
    )
    await connection.execute(
        "INSERT INTO github_repositories (workspace_id, repository_id, full_name) "
        "VALUES ($1, $2, 'acme/vector')",
        WORKSPACE_ID,
        REPOSITORY_ID,
    )

    for issue_id, workspace_id, team_id, title in (
        (ISSUE_ID, WORKSPACE_ID, TEAM_ID, "Fix the OAuth callback"),
        (OTHER_ISSUE_ID, OTHER_WORKSPACE_ID, OTHER_TEAM_ID, "Their private plan"),
    ):
        await connection.execute(
            INSERT_ISSUE_SQL, issue_id, workspace_id, team_id, ISSUE_NUMBER, title
        )

    await connection.execute(
        "INSERT INTO projects (id, workspace_id, name, state) "
        "VALUES ($1, $2, 'Importer', 'planned')",
        PROJECT_ID,
        WORKSPACE_ID,
    )


# --- helpers ----------------------------------------------------------


async def enable(pool, *event_names, workspace_id=WORKSPACE_ID) -> None:
    """Turn events on for a workspace, as an admin's toggle would."""
    for event in event_names:
        await pool.execute(
            "INSERT INTO slack_notification_preferences "
            "(workspace_id, event, enabled) VALUES ($1, $2, TRUE)",
            workspace_id,
            event,
        )


def notifier(pool, web) -> SlackNotifier:
    return SlackNotifier(
        pool=pool,
        events=EventRepository(),
        slack=SlackRepository(),
        token_store=DatabaseTokenStore(),
        web=web,
        base_url=BASE_URL,
    )


def github(pool) -> GithubService:
    """The real service. Nothing here reads a credential: `apply_webhook` runs
    after the signature has been verified, in the transport."""
    return GithubService(
        pool=pool,
        repository=GithubRepository(),
        config=GithubAppConfig(
            app_id="123456",
            private_key="not-a-key",
            webhook_secret="not-a-secret",
            client_id="not-a-client",
            client_secret="not-a-secret-either",
            redirect_allowlist=(),
        ),
    )


def merge_payload(*, action="closed", title=f"Ship it (fixes {TEAM_KEY}-142)"):
    return {
        "action": action,
        "installation": {"id": INSTALLATION_ID},
        "repository": {"id": REPOSITORY_ID, "full_name": "acme/vector"},
        "pull_request": {
            "number": PULL_NUMBER,
            "title": title,
            "body": None,
            "state": "closed",
            "draft": False,
            "merged_at": MERGED_AT,
            "updated_at": MERGED_AT,
            "head": {"ref": "main"},
            "html_url": f"https://github.com/acme/vector/pull/{PULL_NUMBER}",
        },
    }


async def assign(pool, *, issue_id=ISSUE_ID, workspace_id=WORKSPACE_ID, user_id):
    """Assign an issue through the code path that decides what that means.

    `record_changes` is where the product's issue policy already lives -- it
    writes the history rows, subscribes the new assignee and files the inbox
    notification -- so driving the event from anywhere else would be testing a
    second implementation of the rule.
    """
    before = await snapshot(pool, issue_id=issue_id, workspace_id=workspace_id)

    await pool.execute(
        "UPDATE issues SET assignee_id = $3 WHERE workspace_id = $1 AND id = $2",
        workspace_id,
        issue_id,
        user_id,
    )

    async with pool.acquire() as connection:
        async with connection.transaction():
            await record_changes(
                connection,
                scope=WorkspaceScope(workspace_id=workspace_id),
                issue_id=issue_id,
                actor_id=None,
                before=before,
                after=IssueSnapshot(
                    title=before.title,
                    priority=before.priority,
                    workflow_state_id=before.workflow_state_id,
                    assignee_id=user_id,
                    project_id=before.project_id,
                    cycle_id=before.cycle_id,
                ),
            )


async def snapshot(pool, *, issue_id, workspace_id) -> IssueSnapshot:
    row = await pool.fetchrow(
        "SELECT title, priority, workflow_state_id, assignee_id, project_id, "
        "cycle_id FROM issues WHERE workspace_id = $1 AND id = $2",
        workspace_id,
        issue_id,
    )

    return IssueSnapshot(**dict(row))


async def events(pool, *, workspace_id=WORKSPACE_ID) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT kind, dedupe_key, slack_state, slack_failure, slack_attempts, "
        "slack_delivered_at, subject, summary, path FROM domain_events "
        "WHERE workspace_id = $1 ORDER BY kind, dedupe_key",
        workspace_id,
    )


# --- the ordinary path works ------------------------------------------


async def test_an_assignment_reaches_the_workspaces_channel(pool):
    """The feature, before the refusals: this has to work or nothing below
    proves anything.

    One message, in this workspace's channel, presenting this workspace's bot
    token, carrying a deep link to the real Vector route for the issue.
    """
    await enable(pool, "issue_assigned")
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb()

    assert await notifier(pool, web).deliver_pending() == 1

    (post,) = web.posts

    assert post["channel"] == CHANNEL_ID
    assert post["token"] == TOKEN_REFERENCE
    assert post["text"].startswith(f"Assigned · {TEAM_KEY}-{ISSUE_NUMBER} — ")
    assert post["text"].endswith(f"{BASE_URL}/{WORKSPACE_SLUG}/issues/{ISSUE_ID}")

    (row,) = await events(pool)

    assert row["slack_state"] == "delivered"
    assert row["slack_delivered_at"] is not None
    assert row["slack_failure"] is None


async def test_a_delivered_event_is_not_delivered_again(pool):
    """The pending index is the queue, and a delivered row has left it.

    Without this the loop would re-post everything it had ever sent on every
    pass, which is the failure that looks like the integration working.
    """
    await enable(pool, "issue_assigned")
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb()
    delivery = notifier(pool, web)

    await delivery.deliver_pending()

    assert await delivery.deliver_pending() == 0
    assert len(web.posts) == 1


# --- one merge, one message -------------------------------------------


async def test_a_merge_posts_once_however_many_times_github_reports_it(pool):
    """The requirement this pipeline was built around.

    Three deliveries, three DIFFERENT `X-GitHub-Delivery` ids, one merge.
    `github_deliveries` catches a redelivery of the same id and catches nothing
    here: GitHub sends the whole `pull_request` object on every later action
    against an already-merged pull request, each genuinely new. So the third
    delivery below is not a retry at all -- it is an ordinary `edited` event --
    and it is exactly the shape that makes a naive "if merged, post" handler
    announce a merge twice.

    What refuses it is `domain_events_pkey` over (workspace, kind, dedupe key),
    with the key naming the fact: repository, number, issue. One event, one
    post, and the second and third emissions are conflicts absorbed inside the
    transaction that reported them rather than second rows.
    """
    await enable(pool, "pull_request_merged")

    service = github(pool)

    for index, action in enumerate(("closed", "closed", "edited")):
        await service.apply_webhook(
            event="pull_request",
            payload=merge_payload(action=action),
            delivery_id=f"d-{index}",
        )

    assert len(await events(pool)) == 1

    web = FakeWeb()

    assert await notifier(pool, web).deliver_pending() == 1
    assert len(web.posts) == 1
    assert web.posts[0]["text"].endswith(
        f"{BASE_URL}/{WORKSPACE_SLUG}/issues/{ISSUE_ID}"
    )


async def test_a_merge_naming_no_vector_issue_announces_nothing(pool):
    """A pull request with no identifier in it has no Vector route to link to.

    GitHub's own Slack app already posts raw pull-request traffic; a second
    integration repeating it -- with a link into a Vector page that is about
    nothing in particular -- is the noise that gets a channel muted.
    """
    await enable(pool, "pull_request_merged")

    await github(pool).apply_webhook(
        event="pull_request",
        payload=merge_payload(title="Tidy the imports"),
        delivery_id="d-1",
    )

    assert await events(pool) == []


# --- the preference is honoured ---------------------------------------


async def test_a_disabled_preference_posts_nothing(pool):
    """Absence of a preference row means OFF, which is migration 018's opt-in
    rule and the state every workspace starts in.

    The event is still written -- the write path does not consult Slack, which
    is the whole point -- and it is closed as SKIPPED without a call. Asserting
    on `web.posts` rather than only on the row is deliberate: a pipeline that
    posted and then recorded a skip would satisfy a row-only assertion.
    """
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb()

    assert await notifier(pool, web).deliver_pending() == 0
    assert web.posts == []

    (row,) = await events(pool)

    assert row["slack_state"] == "skipped"
    assert row["slack_failure"] == "preference_disabled"
    assert row["slack_delivered_at"] is None


async def test_an_explicit_off_posts_nothing_either(pool):
    """A stored FALSE and a missing row are the same answer to "post this?".

    They are different facts -- 018 keeps the column so that "deliberately
    disabled" is distinguishable from "never configured" -- and this asserts
    the delivery path does not accidentally read one of them as consent.
    """
    await pool.execute(
        "INSERT INTO slack_notification_preferences "
        "(workspace_id, event, enabled) VALUES ($1, 'issue_assigned', FALSE)",
        WORKSPACE_ID,
    )
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb()

    assert await notifier(pool, web).deliver_pending() == 0
    assert web.posts == []


async def test_a_workspace_with_no_channel_chosen_posts_nothing(pool):
    """Connected, permitted, nowhere to post -- a real state, between pressing
    connect and finishing setup. Skipped rather than failed: nothing is wrong,
    somebody simply has not finished."""
    await enable(pool, "issue_assigned")
    await pool.execute(
        "UPDATE slack_notification_settings SET default_channel_id = NULL, "
        "default_channel_name = NULL WHERE workspace_id = $1",
        WORKSPACE_ID,
    )
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb()

    await notifier(pool, web).deliver_pending()

    assert web.posts == []

    (row,) = await events(pool)

    assert (row["slack_state"], row["slack_failure"]) == (
        "skipped",
        "no_default_channel",
    )


# --- a refusal is recorded as one -------------------------------------


async def test_a_refused_post_is_recorded_failed_and_never_delivered(pool):
    """Slack said no, and the row says so in this product's words.

    `is_archived` is Slack's spelling and it reaches exactly one frame --
    `_failure_for` -- which turns it into CHANNEL_UNAVAILABLE. The provider's
    identifier set changes without notice and is not in this schema.

    The assertion that matters most is the last one: `slack_delivered_at` is
    still NULL. A pipeline that caught the exception and recorded a delivery
    would be refused by `domain_events_delivered_has_an_instant`, and this is
    the test that would notice if that constraint were ever relaxed.
    """
    await enable(pool, "issue_assigned")
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb(post_error=SlackApiError("is_archived"))

    assert await notifier(pool, web).deliver_pending() == 0
    assert len(web.posts) == 1

    (row,) = await events(pool)

    assert row["slack_state"] == "failed"
    assert row["slack_failure"] == "channel_unavailable"
    assert row["slack_delivered_at"] is None


async def test_a_failed_event_is_not_retried(pool):
    """A refusal Slack articulated stays true until a person changes something.

    Retrying an archived channel is how a dead integration spends a workspace's
    rate limit forever, and how the same failure reaches an operator's log a
    thousand times.
    """
    await enable(pool, "issue_assigned")
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb(post_error=SlackApiError("is_archived"))
    delivery = notifier(pool, web)

    await delivery.deliver_pending()
    await delivery.deliver_pending()

    assert len(web.posts) == 1


async def test_a_transient_failure_stays_pending_and_is_not_due_yet(pool):
    """No answer from Slack is not a refusal, and must not be recorded as one.

    The row stays PENDING with the attempt counted and the next attempt pushed
    into the future -- which is what a retry cannot hot-loop past, because the
    same statement that counted the attempt moved the clock.

    No failure reason is written, and `domain_events_progress_has_no_reason`
    would refuse one: a momentary blip must not read as a broken integration on
    every screen that tallies the column.
    """
    await enable(pool, "issue_assigned")
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb(post_error=SlackApiError())
    delivery = notifier(pool, web)

    await delivery.deliver_pending()

    # A second pass immediately afterwards finds nothing due, which is the
    # backoff working rather than the queue being empty.
    await delivery.deliver_pending()

    assert len(web.posts) == 1

    row = await pool.fetchrow(
        "SELECT slack_state, slack_failure, slack_attempts, "
        "slack_next_attempt_at > now() AS waiting FROM domain_events"
    )

    assert row["slack_state"] == "pending"
    assert row["slack_failure"] is None
    assert row["slack_attempts"] == 1
    assert row["waiting"] is True


async def test_a_transient_failure_becomes_a_recorded_one_eventually(pool):
    """Bounded, so an unreachable Slack does not mean an unbounded retry.

    The recorded reason is Slack's real one and not a synthetic "gave up": an
    operator needs to know WHAT kept failing, and how many times is already in
    `slack_attempts`.
    """
    await enable(pool, "issue_assigned")
    await assign(pool, user_id=MEMBER_ID)

    # Fast-forwarded rather than looped over five one-minute backoffs. What is
    # under test is the ceiling, not the arithmetic that reaches it -- and the
    # arithmetic is asserted by the test above.
    await pool.execute(
        "UPDATE domain_events SET slack_attempts = $1, slack_next_attempt_at = now()",
        MAX_ATTEMPTS - 1,
    )

    web = FakeWeb(post_error=SlackApiError())

    await notifier(pool, web).deliver_pending()

    (row,) = await events(pool)

    assert row["slack_state"] == "failed"
    assert row["slack_failure"] == "slack_unreachable"
    assert row["slack_attempts"] == MAX_ATTEMPTS
    assert row["slack_delivered_at"] is None


async def test_a_declined_scope_is_a_failure_rather_than_a_skip(pool):
    """An admin who declined `chat:write` has a half-installed integration.

    Checked against what Slack GRANTED and never against what this release
    requests, and reported as a failure because -- unlike a disabled
    preference -- somebody has to go and reconnect. No call is made: a post
    that cannot succeed should not spend a request to find out.
    """
    await enable(pool, "issue_assigned")
    await pool.execute(
        "UPDATE slack_installations SET scopes = ARRAY['channels:read'] "
        "WHERE workspace_id = $1",
        WORKSPACE_ID,
    )
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb()

    await notifier(pool, web).deliver_pending()

    assert web.posts == []

    (row,) = await events(pool)

    assert (row["slack_state"], row["slack_failure"]) == ("failed", "missing_scope")


# --- tenancy ----------------------------------------------------------


async def test_one_workspaces_event_never_reaches_another_workspaces_channel(pool):
    """The property migration 027's opening paragraph is about.

    Both workspaces have an event pending and both are connected to Slack with
    a channel and a bot of their own, so a delivery path that read a channel
    from anywhere but the event's own workspace has somewhere wrong to send it.

    Asserted on the PAIRING and not just on the set of channels used. Two
    messages arriving in two channels would satisfy a weaker check even if they
    had been swapped -- which is precisely the bug, and the one that puts one
    company's issue titles into another company's Slack.
    """
    await enable(pool, "issue_assigned")
    await enable(pool, "issue_assigned", workspace_id=OTHER_WORKSPACE_ID)

    await assign(pool, user_id=MEMBER_ID)
    await assign(
        pool,
        issue_id=OTHER_ISSUE_ID,
        workspace_id=OTHER_WORKSPACE_ID,
        user_id=OUTSIDER_ID,
    )

    web = FakeWeb()

    assert await notifier(pool, web).deliver_pending() == 2

    by_channel = {post["channel"]: post for post in web.posts}

    assert set(by_channel) == {CHANNEL_ID, OTHER_CHANNEL_ID}

    ours = by_channel[CHANNEL_ID]
    theirs = by_channel[OTHER_CHANNEL_ID]

    assert ours["token"] == TOKEN_REFERENCE
    assert theirs["token"] == OTHER_TOKEN_REFERENCE

    assert f"{TEAM_KEY}-{ISSUE_NUMBER}" in ours["text"]
    assert "Their private plan" not in ours["text"]
    assert "Fix the OAuth callback" not in theirs["text"]


async def test_a_workspace_that_never_connected_slack_is_skipped_not_failed(pool):
    """A deployment where nobody uses Slack must not accumulate pending rows.

    Skipped within seconds keeps `domain_events_pending_idx` empty and keeps
    the record honest -- nothing was sent, and nothing was going to be -- while
    leaving `not_connected` to mean what Slack means by it, which is a token
    that has stopped working.
    """
    await pool.execute(
        "DELETE FROM slack_notification_settings WHERE workspace_id = $1",
        WORKSPACE_ID,
    )
    await pool.execute(
        "DELETE FROM slack_channels WHERE workspace_id = $1", WORKSPACE_ID
    )
    await pool.execute(
        "DELETE FROM slack_installations WHERE workspace_id = $1", WORKSPACE_ID
    )
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb()

    await notifier(pool, web).deliver_pending()

    assert web.posts == []

    (row,) = await events(pool)

    assert (row["slack_state"], row["slack_failure"]) == (
        "skipped",
        "no_installation",
    )


# --- the connection is not held ---------------------------------------


async def test_the_delivery_holds_no_connection_while_it_talks_to_slack(pool):
    """A pool exhausted by an integration is an outage in the rest of the app.

    The pool has exactly one connection and the fake reaches into it from
    inside `post_message`. A notifier that claimed a row and kept its
    connection across the call would leave nothing for that acquire to take,
    and `wait_for` turns the resulting hang into a failure rather than a test
    that never finishes.
    """
    await enable(pool, "issue_assigned")
    await assign(pool, user_id=MEMBER_ID)

    web = FakeWeb(pool=pool)

    delivered = await asyncio.wait_for(
        notifier(pool, web).deliver_pending(),
        timeout=10,
    )

    assert delivered == 1


# --- projects ---------------------------------------------------------


async def test_a_project_update_announces_the_health_move_and_the_update(pool):
    """Two events for one update, because a workspace toggles them separately.

    A lead watching for status changes and a team reading written updates want
    different things, and 018 gives them two switches. Collapsing them here
    would be choosing on their behalf.
    """
    await enable(pool, "project_health_changed", "project_update_published")

    await projects(pool).post_update(
        scope=WorkspaceScope(workspace_id=WORKSPACE_ID),
        project_id=PROJECT_ID,
        health="at_risk",
        body="Blocked on the vendor\nmore detail here",
        author_id=MEMBER_ID,
    )

    web = FakeWeb()

    assert await notifier(pool, web).deliver_pending() == 2

    texts = sorted(post["text"] for post in web.posts)

    assert texts[0].startswith("Project health · Importer — Now at risk")
    assert texts[1].startswith("Project update · Importer — Blocked on the vendor")
    assert all(
        f"{BASE_URL}/{WORKSPACE_SLUG}/projects/{PROJECT_ID}" in text for text in texts
    )


async def test_a_second_update_at_the_same_health_announces_no_move(pool):
    """ "Health changed" has to mean it changed.

    A weekly update that reports the same status is a report, not a change, and
    a channel that announced "still at risk" every Friday is the noise 018
    keeps its vocabulary short to avoid. The written update is still announced,
    because that IS new.
    """
    await enable(pool, "project_health_changed", "project_update_published")

    service = projects(pool)
    scope = WorkspaceScope(workspace_id=WORKSPACE_ID)

    for body in ("First", "Second"):
        await service.post_update(
            scope=scope,
            project_id=PROJECT_ID,
            health="at_risk",
            body=body,
            author_id=MEMBER_ID,
        )

    kinds = [row["kind"] for row in await events(pool)]

    assert kinds.count("project_health_changed") == 1
    assert kinds.count("project_update_published") == 2


def projects(pool) -> ProjectService:
    return ProjectService(
        pool=pool,
        repository=ProjectRepository(),
        issue_repository=IssueRepository(),
        initiative_repository=InitiativeRepository(),
    )


# --- the loop's own guarantee -----------------------------------------


async def test_a_claim_pushes_the_next_attempt_out_in_the_same_statement(pool):
    """The retry floor, asserted at the repository rather than through a loop.

    This is the statement everything else about hot-looping rests on: whatever
    the caller does afterwards -- crash, forget, run twelve copies -- the row is
    not due again until the delay has passed, because the increment and the
    reschedule are one write.
    """
    await enable(pool, "issue_assigned")
    await assign(pool, user_id=MEMBER_ID)

    repository = EventRepository()

    async with pool.acquire() as connection:
        async with connection.transaction():
            claimed = await repository.claim_next(
                connection,
                retry_delay=timedelta(minutes=1),
                backoff_steps=5,
            )

    assert claimed is not None
    assert claimed.attempts == 1

    # And nothing is due to a second claimer, which is what stops two drainers
    # posting the same message twice.
    async with pool.acquire() as connection:
        async with connection.transaction():
            assert (
                await repository.claim_next(
                    connection,
                    retry_delay=timedelta(minutes=1),
                    backoff_steps=5,
                )
                is None
            )
