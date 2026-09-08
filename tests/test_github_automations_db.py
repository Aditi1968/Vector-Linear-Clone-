"""The status automation against a real schema, and the Slack path end to end.

Three things are asked here that no fake can answer.

WHAT MIGRATION 030 REFUSES. A composite state reference that would let one
team's automation name another team's board; a row that moves nothing; a
`caused_by` longer than the column allows.

WHAT THE REAL UPDATE DOES. `move_issues_for_automation` is one statement doing
a per-issue join to a per-team configuration, with a forward-only category
predicate and the `completed_at` rule recomputed inline. Every one of those is
SQL, and the fake in tests/test_github_automations.py imitates them rather than
running them -- so the interesting cases (two teams in one payload, an issue
already at the target, an archived issue) are pinned here.

WHETHER SLACK ACTUALLY RECEIVES A MESSAGE. tests/test_notification_pipeline_db
proves the pipeline down to the `SlackWebApi` seam, and tests/test_slack_
channels proves `SlackWebClient.post_message` builds the right HTTP request --
but nothing joined the two, so nothing proved that a domain event ends in a
POST to chat.postMessage through the code a deployment actually runs. The last
section of this file is that join: a real GitHub delivery, the real automation,
the real `SlackNotifier`, the real `SlackWebClient`, and an `httpx.MockTransport`
in place of the socket. No Slack credential and no network.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import json
from uuid import UUID

import asyncpg
import httpx
import pytest

from app.domain.tenancy import AuthorizedWorkspaceScope, WorkspaceScope
from app.repositories.events import EventRepository
from app.repositories.github import GithubRepository
from app.repositories.slack import SlackRepository
from app.services.github import GithubAppConfig, GithubService
from app.services.notifications import SlackNotifier
from app.services.slack import (
    SLACK_CHAT_POST_MESSAGE_URL,
    DatabaseTokenStore,
    SlackWebClient,
)

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

# A second team in the SAME workspace, with its own board. The point of it is
# the pull request titled "fixes CORE-142, WEB-3": two teams, two
# configurations, one statement.
WEB_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")
WEB_TEAM_KEY = "WEB"

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")

ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")
WEB_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b2")
ARCHIVED_ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b3")

INSTALLATION_ID = 4242
REPOSITORY_ID = 11111
OTHER_REPOSITORY_ID = 22222
PULL_NUMBER = 84

ISSUE_NUMBER = 142
WEB_ISSUE_NUMBER = 3

MERGED_AT = "2026-04-01T10:00:00Z"
UPDATED_AT = "2026-04-01T09:00:00Z"

CHANNEL_ID = "C0ENGINEERING"

# `DatabaseTokenStore` keeps the token IN the reference column -- the reference
# IS the token, as that class says -- so this placeholder is what the bearer
# header ends up carrying. Obviously not a credential, and never sent anywhere:
# the socket is replaced.
TOKEN_REFERENCE = "xoxb-not-a-real-token"
BASE_URL = "https://app.vector.test"

SCOPE = WorkspaceScope(workspace_id=WORKSPACE_ID)
ADMIN_SCOPE = AuthorizedWorkspaceScope(
    workspace_id=WORKSPACE_ID,
    user_id=MEMBER_ID,
    role="admin",
)

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
    id, workspace_id, team_id, number, workflow_state_id, title, priority,
    archived_at
)
VALUES (
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3 AND type = $6
    ),
    $5, 1, NULL
)
"""

INSERT_SLACK_INSTALLATION_SQL = """
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
VALUES ($1, 'T0OURS', 'Ours', 'U0BOTBOT',
    ARRAY['channels:read', 'chat:write'], 'database', $2, $3)
"""


@pytest.fixture
async def pool(postgres_dsn):
    """A pool of ONE over a fully migrated database.

    One connection, for the reason tests/test_notification_pipeline_db.py
    gives: the Slack section below would deadlock instantly if anything held a
    connection across the HTTP call, which turns that rule into a proof rather
    than a comment.
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
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, 'Web', $3)",
        WEB_TEAM_ID,
        WORKSPACE_ID,
        WEB_TEAM_KEY,
    )
    await seed_workflow_states(connection, WORKSPACE_ID, WEB_TEAM_ID)

    # A SECOND started state on the bootstrap team, which is the case the whole
    # "which one?" question is about: 005 seeds one of each category, and a real
    # team adds "In Review" the first week.
    await connection.execute(
        "INSERT INTO workflow_states (workspace_id, team_id, name, type, position) "
        "VALUES ($1, $2, 'In Review', 'started', 9)",
        WORKSPACE_ID,
        TEAM_ID,
    )

    await connection.execute(INSERT_USER_SQL, MEMBER_ID)
    await connection.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role) "
        "VALUES ($1, $2, 'admin')",
        WORKSPACE_ID,
        MEMBER_ID,
    )
    await connection.execute(
        "INSERT INTO github_installations "
        "(workspace_id, installation_id, connected_by, confirmed_at) "
        "VALUES ($1, $2, $3, now())",
        WORKSPACE_ID,
        INSTALLATION_ID,
        MEMBER_ID,
    )

    for repository_id, full_name in (
        (REPOSITORY_ID, "acme/vector"),
        (OTHER_REPOSITORY_ID, "acme/docs"),
    ):
        await connection.execute(
            "INSERT INTO github_repositories (workspace_id, repository_id, full_name) "
            "VALUES ($1, $2, $3)",
            WORKSPACE_ID,
            repository_id,
            full_name,
        )

    for issue_id, team_id, number, title, category in (
        (ISSUE_ID, TEAM_ID, ISSUE_NUMBER, "Fix the OAuth callback", "unstarted"),
        (WEB_ISSUE_ID, WEB_TEAM_ID, WEB_ISSUE_NUMBER, "Fix the header", "unstarted"),
        (ARCHIVED_ISSUE_ID, TEAM_ID, 900, "Old", "unstarted"),
    ):
        await connection.execute(
            INSERT_ISSUE_SQL, issue_id, WORKSPACE_ID, team_id, number, title, category
        )

    await connection.execute(
        "UPDATE issues SET archived_at = now() WHERE id = $1", ARCHIVED_ISSUE_ID
    )

    # The Slack side, for the last section of this file.
    await connection.execute(
        INSERT_SLACK_INSTALLATION_SQL, WORKSPACE_ID, TOKEN_REFERENCE, MEMBER_ID
    )
    await connection.execute(
        "INSERT INTO slack_channels (workspace_id, channel_id, name) "
        "VALUES ($1, $2, 'eng')",
        WORKSPACE_ID,
        CHANNEL_ID,
    )
    await connection.execute(
        "INSERT INTO slack_notification_settings "
        "(workspace_id, default_channel_id, default_channel_name) "
        "VALUES ($1, $2, 'eng')",
        WORKSPACE_ID,
        CHANNEL_ID,
    )


# --- helpers ----------------------------------------------------------


def build_service(pool) -> GithubService:
    """The real service over the real repository and the real pool.

    Nothing on these paths reads a credential: `apply_webhook` runs after the
    signature has been verified, in the transport.
    """
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


def pull_payload(
    *,
    action="opened",
    title=f"Fix {TEAM_KEY}-{ISSUE_NUMBER}",
    state="open",
    draft=False,
    merged_at=None,
    updated_at=UPDATED_AT,
    repository_id=REPOSITORY_ID,
):
    return {
        "action": action,
        "installation": {"id": INSTALLATION_ID},
        "repository": {"id": repository_id, "full_name": "acme/vector"},
        "pull_request": {
            "number": PULL_NUMBER,
            "title": title,
            "body": None,
            "state": state,
            "draft": draft,
            "merged_at": merged_at,
            "updated_at": updated_at,
            "head": {"ref": "main"},
            "html_url": f"https://github.com/acme/vector/pull/{PULL_NUMBER}",
        },
    }


def merge_payload(**overrides):
    return pull_payload(
        action="closed",
        state="closed",
        merged_at=MERGED_AT,
        updated_at=MERGED_AT,
        **overrides,
    )


async def state_id(pool, *, team_id, category, name=None):
    return await pool.fetchval(
        """
        SELECT id FROM workflow_states
        WHERE workspace_id = $1 AND team_id = $2 AND type = $3
          AND ($4::TEXT IS NULL OR name = $4)
        ORDER BY position, id
        LIMIT 1
        """,
        WORKSPACE_ID,
        team_id,
        category,
        name,
    )


async def category_of(pool, issue_id=ISSUE_ID):
    return await pool.fetchval(
        """
        SELECT states.type
        FROM issues
        JOIN workflow_states AS states
          ON states.id = issues.workflow_state_id
        WHERE issues.id = $1
        """,
        issue_id,
    )


async def automate(pool, team_id=TEAM_ID, **kwargs):
    await build_service(pool).set_issue_automation(
        ADMIN_SCOPE, team_id=team_id, enabled=True, **kwargs
    )


async def deliver(pool, payload, delivery_id="d-1"):
    await build_service(pool).apply_webhook(
        event="pull_request", payload=payload, delivery_id=delivery_id
    )


# --- the schema -------------------------------------------------------


async def test_an_automation_cannot_name_another_teams_workflow_state(pool):
    """The composite reference, which is the whole tenancy story of 030.

    Without it the row stores fine and the DELIVERY fails -- on
    `issues_workflow_state_fk`, inside a webhook transaction, as a 500 GitHub
    redelivers forever.
    """
    foreign = await state_id(pool, team_id=WEB_TEAM_ID, category="started")

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await pool.execute(
            "INSERT INTO github_issue_automations "
            "(workspace_id, team_id, started_state_id) VALUES ($1, $2, $3)",
            WORKSPACE_ID,
            TEAM_ID,
            foreign,
        )


async def test_an_automation_that_moves_nothing_is_refused(pool):
    """A row of two NULLs means what no row means, and the absence of a row is
    already how the automation is turned off."""
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO github_issue_automations (workspace_id, team_id) "
            "VALUES ($1, $2)",
            WORKSPACE_ID,
            TEAM_ID,
        )


async def test_a_workflow_state_an_automation_names_cannot_be_deleted(pool):
    """RESTRICT: reconfiguring the automation is the way out, not a silently
    dangling reference."""
    await automate(pool)

    started = await state_id(pool, team_id=TEAM_ID, category="started")

    # RestrictViolationError and not ForeignKeyViolationError: PostgreSQL
    # reports NO ACTION as the latter and RESTRICT as the former, and the two
    # are siblings under IntegrityConstraintViolationError rather than one
    # being the other. Pinning the precise class is what makes this a test of
    # the referential ACTION 030 chose rather than of the key existing.
    with pytest.raises(asyncpg.RestrictViolationError):
        await pool.execute("DELETE FROM workflow_states WHERE id = $1", started)


async def test_an_activity_cause_is_bounded(pool):
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "INSERT INTO issue_activity "
            "(workspace_id, issue_id, kind, caused_by) VALUES ($1, $2, 'created', $3)",
            WORKSPACE_ID,
            ISSUE_ID,
            "x" * 201,
        )


async def test_every_repository_starts_tracked(pool):
    """The compatibility story of the column: a workspace that never opens the
    setting behaves exactly as it did before 030."""
    tracked = await pool.fetch(
        "SELECT tracked FROM github_repositories WHERE workspace_id = $1",
        WORKSPACE_ID,
    )

    assert [row["tracked"] for row in tracked] == [True, True]


# --- the real move ----------------------------------------------------


async def test_an_opened_pull_request_starts_the_issue(pool):
    await automate(pool)

    await deliver(pool, pull_payload())

    assert await category_of(pool) == "started"


async def test_a_merge_completes_the_issue_and_stamps_completed_at(pool):
    """`completed_at` is derived and lives at the write, not in a constraint --
    the two columns are in different tables. A move that skipped it would leave
    an issue in Done that every report over shipping dates got wrong."""
    await automate(pool)

    await deliver(pool, merge_payload())

    row = await pool.fetchrow("SELECT completed_at FROM issues WHERE id = $1", ISSUE_ID)

    assert await category_of(pool) == "completed"
    assert row["completed_at"] is not None


async def test_reopening_a_completed_issues_pull_request_does_not_uncomplete_it(pool):
    """Forward-only, from the other end: `started` is not below `completed`."""
    await automate(pool)
    await deliver(pool, merge_payload())

    await deliver(
        pool,
        pull_payload(action="reopened", updated_at="2026-04-01T12:00:00Z"),
        delivery_id="d-2",
    )

    assert await category_of(pool) == "completed"


async def test_one_pull_request_moves_two_teams_issues_by_their_own_boards(pool):
    """The statement joins the configuration per issue, which is why the target
    cannot be an argument: two teams, two boards, one delivery."""
    await automate(pool)
    await automate(
        pool,
        team_id=WEB_TEAM_ID,
        completed_state_id=await state_id(
            pool, team_id=WEB_TEAM_ID, category="completed"
        ),
    )

    await deliver(
        pool,
        merge_payload(
            title=f"Ship it (fixes {TEAM_KEY}-{ISSUE_NUMBER}, "
            f"{WEB_TEAM_KEY}-{WEB_ISSUE_NUMBER})"
        ),
    )

    assert await category_of(pool) == "completed"
    assert await category_of(pool, WEB_ISSUE_ID) == "completed"


async def test_an_issue_on_a_team_with_no_automation_is_left_alone(pool):
    await automate(pool)

    await deliver(
        pool,
        merge_payload(
            title=f"Ship it (fixes {TEAM_KEY}-{ISSUE_NUMBER}, "
            f"{WEB_TEAM_KEY}-{WEB_ISSUE_NUMBER})"
        ),
    )

    assert await category_of(pool, WEB_ISSUE_ID) == "unstarted"


async def test_the_history_records_the_pull_request_and_no_actor(pool):
    """The audit trail. An issue that moved with no actor and no reason is
    worse than one that did not move."""
    await automate(pool)

    await deliver(pool, pull_payload())

    row = await pool.fetchrow(
        "SELECT actor_id, kind, from_value, to_value, caused_by FROM issue_activity "
        "WHERE issue_id = $1 ORDER BY created_at DESC, id DESC LIMIT 1",
        ISSUE_ID,
    )

    assert row["kind"] == "state_changed"
    assert row["actor_id"] is None
    assert row["caused_by"] == f"github_pull_request:acme/vector#{PULL_NUMBER}"
    assert row["from_value"] != row["to_value"]


async def test_the_watchers_are_notified_by_a_move_with_no_actor(pool):
    """An automated move files an inbox item exactly as a person's move does.

    The constraint this is really about is
    `notifications_actor_is_not_recipient`. The statement excludes the actor
    with `IS DISTINCT FROM`, and a NULL actor is distinct from everybody -- so
    nobody is spared, which is right, because nobody in this workspace did it.
    A CHECK violation here would abort the delivery's transaction and reach
    GitHub as a 500, which is an unbounded redelivery loop.
    """
    await pool.execute(
        "UPDATE issues SET assignee_id = $2 WHERE id = $1", ISSUE_ID, MEMBER_ID
    )
    await automate(pool)

    await deliver(pool, pull_payload())

    row = await pool.fetchrow(
        "SELECT user_id, actor_id, kind FROM notifications WHERE issue_id = $1",
        ISSUE_ID,
    )

    assert row["user_id"] == MEMBER_ID
    assert row["actor_id"] is None
    assert row["kind"] == "status_changed"


async def test_a_second_open_delivery_writes_no_second_history_row(pool):
    """Idempotency where it is visible: not "the state is still right", but
    "the timeline does not say it happened twice"."""
    await automate(pool)

    await deliver(pool, pull_payload())
    await deliver(
        pool,
        pull_payload(action="reopened", updated_at="2026-04-01T12:00:00Z"),
        delivery_id="d-2",
    )

    rows = await pool.fetchval(
        "SELECT count(*) FROM issue_activity "
        "WHERE issue_id = $1 AND kind = 'state_changed'",
        ISSUE_ID,
    )

    assert rows == 1


async def test_a_person_who_moved_the_issue_on_is_not_dragged_back(pool):
    """A human puts CORE-142 in "In Review" -- a SECOND started state -- while
    the pull request is open. The next delivery must leave it there."""
    await automate(pool)
    await deliver(pool, pull_payload())

    in_review = await state_id(
        pool, team_id=TEAM_ID, category="started", name="In Review"
    )
    await pool.execute(
        "UPDATE issues SET workflow_state_id = $2 WHERE id = $1", ISSUE_ID, in_review
    )

    await deliver(
        pool,
        pull_payload(action="reopened", updated_at="2026-04-01T12:00:00Z"),
        delivery_id="d-2",
    )

    placed = await pool.fetchval(
        "SELECT workflow_state_id FROM issues WHERE id = $1", ISSUE_ID
    )

    assert placed == in_review


async def test_an_archived_issue_is_never_moved(pool):
    """A pull-request title is not the way back into the product."""
    await automate(pool)

    await deliver(pool, pull_payload(title=f"Fix {TEAM_KEY}-900"))

    assert await category_of(pool, ARCHIVED_ISSUE_ID) == "unstarted"


async def test_a_draft_moves_nothing_and_still_stores_the_pull_request(pool):
    """The refusal is about the ISSUE, not about the delivery: the Development
    section still shows the draft."""
    await automate(pool)

    await deliver(pool, pull_payload(draft=True))

    stored = await pool.fetchval(
        "SELECT draft FROM github_pull_requests WHERE number = $1", PULL_NUMBER
    )

    assert stored is True
    assert await category_of(pool) == "unstarted"


# --- choosing repositories --------------------------------------------


async def test_an_untracked_repository_has_its_deliveries_dropped(pool):
    await automate(pool)

    await build_service(pool).set_tracked_repositories(
        ADMIN_SCOPE, repository_ids=[OTHER_REPOSITORY_ID]
    )

    await deliver(pool, pull_payload())

    assert await category_of(pool) == "unstarted"
    assert await pool.fetchval("SELECT count(*) FROM github_pull_requests") == 0


async def test_tracking_it_again_lets_deliveries_through(pool):
    """The setting is a filter and not a deletion; nothing had to be
    reconnected to undo it."""
    service = build_service(pool)

    await automate(pool)
    await service.set_tracked_repositories(ADMIN_SCOPE, repository_ids=[])
    await service.set_tracked_repositories(ADMIN_SCOPE, repository_ids=[REPOSITORY_ID])

    await deliver(pool, pull_payload())

    assert await category_of(pool) == "started"


async def test_the_defaults_come_from_the_board_and_are_stored_explicitly(pool):
    """The team has TWO started states. The first by board order is chosen, and
    the id is written down -- so this screen shows a named state rather than a
    rule that will pick one later."""
    await automate(pool)

    row = await pool.fetchrow(
        "SELECT started_state_id, completed_state_id FROM github_issue_automations "
        "WHERE workspace_id = $1 AND team_id = $2",
        WORKSPACE_ID,
        TEAM_ID,
    )

    assert row["started_state_id"] == await state_id(
        pool, team_id=TEAM_ID, category="started", name="In Progress"
    )
    assert row["completed_state_id"] == await state_id(
        pool, team_id=TEAM_ID, category="completed"
    )


# --- Slack, end to end, with the socket replaced ----------------------


def slack_notifier(pool) -> SlackNotifier:
    """The composition `app/main.py` builds, with the REAL web client.

    `SlackWebClient` is what a deployment runs and what
    tests/test_notification_pipeline_db.py substitutes; using it here is the
    whole point of this section. Nothing about it is stubbed -- the request is
    built, the bearer header is set, the response is decoded and `ok` is
    checked. Only the socket is replaced, by `mock_socket` below.
    """
    return SlackNotifier(
        pool=pool,
        events=EventRepository(),
        slack=SlackRepository(),
        token_store=DatabaseTokenStore(),
        web=SlackWebClient(),
        base_url=BASE_URL,
    )


def mock_socket(monkeypatch, handler):
    """Point every AsyncClient `app.services.slack` builds at `handler`.

    The same seam tests/test_slack_channels.py uses, and for the same reason:
    `SlackWebClient` opens its own client per call -- deliberately, so the
    token is a local rather than instance state -- which leaves no transport to
    inject. Patching the module's `httpx` is the honest way in.
    """
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(**kwargs, transport=transport),
    )


async def enable(pool, event) -> None:
    await pool.execute(
        "INSERT INTO slack_notification_preferences (workspace_id, event, enabled) "
        "VALUES ($1, $2, TRUE)",
        WORKSPACE_ID,
        event,
    )


async def test_an_automated_completion_ends_in_a_post_to_chat_post_message(
    pool, monkeypatch
):
    """THE end-to-end claim, and the one nothing in this repository made before.

    A signed GitHub delivery -> the automation moves the issue to the team's
    completed state -> `record_issue_event` writes an `issue_completed` row in
    the same transaction -> the delivery loop claims it -> the real
    `SlackWebClient` posts it. Every hop is production code; the only thing
    replaced is the socket.
    """
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)

        return httpx.Response(200, json={"ok": True})

    mock_socket(monkeypatch, handler)

    await enable(pool, "issue_completed")
    await automate(pool)

    await deliver(pool, merge_payload())

    delivered = await slack_notifier(pool).deliver_pending()

    assert delivered == 1
    assert [str(request.url) for request in requests] == [SLACK_CHAT_POST_MESSAGE_URL]

    body = json.loads(requests[0].content)

    assert body["channel"] == CHANNEL_ID
    assert f"{TEAM_KEY}-{ISSUE_NUMBER}" in body["text"]
    assert BASE_URL in body["text"]
    assert requests[0].headers["Authorization"].startswith("Bearer ")


async def test_the_event_is_marked_delivered_only_after_slack_said_ok(
    pool, monkeypatch
):
    mock_socket(monkeypatch, lambda request: httpx.Response(200, json={"ok": True}))

    await enable(pool, "issue_completed")
    await automate(pool)
    await deliver(pool, merge_payload())

    await slack_notifier(pool).deliver_pending()

    row = await pool.fetchrow(
        "SELECT slack_state, slack_delivered_at, slack_failure "
        "FROM domain_events WHERE kind = 'issue_completed'"
    )

    assert row["slack_state"] == "delivered"
    assert row["slack_delivered_at"] is not None
    assert row["slack_failure"] is None


async def test_a_refusal_slack_articulated_stops_that_event_and_nothing_else(
    pool, monkeypatch
):
    """The failure arm, which is the one that decides whether a bad row stops
    the queue for everybody.

    `not_in_channel` is a refusal that stays true until a person acts -- the
    bot has not been invited -- so it is recorded rather than retried, and the
    next pass is free to move on.
    """
    mock_socket(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"ok": False, "error": "not_in_channel"}
        ),
    )

    await enable(pool, "issue_completed")
    await automate(pool)
    await deliver(pool, merge_payload())

    delivered = await slack_notifier(pool).deliver_pending()

    row = await pool.fetchrow(
        "SELECT slack_state, slack_failure, slack_delivered_at "
        "FROM domain_events WHERE kind = 'issue_completed'"
    )

    assert delivered == 0
    assert row["slack_state"] == "failed"
    assert row["slack_failure"] == "channel_unavailable"
    assert row["slack_delivered_at"] is None


async def test_a_workspace_that_did_not_enable_the_event_gets_no_request(
    pool, monkeypatch
):
    """Absence of a preference row is OFF, and the check happens before the
    network call rather than after it."""
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)

        return httpx.Response(200, json={"ok": True})

    mock_socket(monkeypatch, handler)

    await automate(pool)
    await deliver(pool, merge_payload())

    assert await slack_notifier(pool).deliver_pending() == 0
    assert requests == []


async def test_an_automated_start_announces_nothing(pool, monkeypatch):
    """`issue_completed` is emitted by a join inside the statement, so a move
    to a started state writes no event at all -- and a channel that announced
    "started" for every opened pull request is the noise nobody asked for."""
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)

        return httpx.Response(200, json={"ok": True})

    mock_socket(monkeypatch, handler)

    await enable(pool, "issue_completed")
    await automate(pool)

    await deliver(pool, pull_payload())

    assert await slack_notifier(pool).deliver_pending() == 0
    assert requests == []
