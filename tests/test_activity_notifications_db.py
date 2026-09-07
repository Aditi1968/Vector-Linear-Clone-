"""Activity and notifications against a real PostgreSQL 18, on the whole chain.

Migration 012 and the services above it make four claims that no fake
connection can check, because every one of them is about what the SERVER does
with a transaction it is holding:

  * an activity row and the change it describes commit together or not at all.
    The test for it is the honest one: make the history write fail, and assert
    the change is gone too;
  * nobody is notified of their own action -- enforced in the statement that
    selects recipients AND by `notifications_actor_is_not_recipient`, so the
    row is not merely absent, it is unwritable;
  * marking read is idempotent, because `COALESCE(read_at, now())` keeps the
    first instant rather than moving it;
  * an inbox is keyed on (workspace, user) TOGETHER, so a member of one
    workspace reads nothing from another, and cannot mark a colleague's
    notification read even inside their own.

Every one of them is attempted through the real services over a real pool,
never by driving a repository directly: the transaction boundaries and the
constraint translation are half the behaviour under test.

Marked `db`: deselected by default, skipped when Docker is unreachable.
Nothing here touches DATABASE_URL or Neon; the only server it speaks to is the
throwaway container `postgres_dsn` starts.
"""

from dataclasses import dataclass
from uuid import UUID, uuid4

import asyncpg
import pytest

from app.domain.activity import ActivityKind
from app.domain.errors import ValidationError
from app.domain.issues import IssuePatch
from app.domain.notifications import NotificationKind
from app.domain.relations import RelationType
from app.domain.tenancy import AuthorizedWorkspaceScope, WorkspaceScope
from app.repositories.activity import ActivityRepository
from app.repositories.comments import CommentRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.issues import IssueRepository
from app.repositories.label_groups import LabelGroupRepository
from app.repositories.labels import LabelRepository
from app.repositories.notifications import NotificationRepository
from app.repositories.relations import RelationRepository
from app.repositories.teams import TeamRepository
from app.services.activity import ActivityService
from app.services.comments import CommentService
from app.services.issues import IssueService
from app.services.labels import LabelService
from app.services.relations import RelationService
from app.services.teams import TeamService

from tests.conftest import (
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

# Workspace A is the tenant 002 seeds, named as a literal in that file
# precisely so a test can assert against a constant. Workspace B is this
# file's own, because isolation is not expressible with one tenant.
WORKSPACE_A = UUID("00000000-0000-7000-8000-000000000001")
TEAM_A = UUID("00000000-0000-7000-8000-000000000002")

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000b1")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000b2")

SCOPE_A = WorkspaceScope(workspace_id=WORKSPACE_A)
SCOPE_B = WorkspaceScope(workspace_id=WORKSPACE_B)

# Three members of A and one of B. Three, because the inbox rules need an
# actor, a recipient and a bystander at once: two people cannot distinguish
# "everyone but the actor" from "the other person".
ALICE = UUID("00000000-0000-7000-8000-0000000000c1")
BOB = UUID("00000000-0000-7000-8000-0000000000c2")
DAVE = UUID("00000000-0000-7000-8000-0000000000c3")
CARA = UUID("00000000-0000-7000-8000-0000000000c4")
# A second member of B, because "nobody is notified of their own action"
# makes a one-person workspace unable to produce a notification at all --
# and an isolation test that asserted an empty inbox on both sides would
# pass against a feature that notifies nobody anywhere.
ERIN = UUID("00000000-0000-7000-8000-0000000000c5")

# ASSIGNED to Alice, FILED by Bob -- the two roles a comment notifies, held by
# different people, so a test can tell which of them a row is for.
ISSUE_A = UUID("00000000-0000-7000-8000-0000000000d1")
# Bob's own work in the same workspace, for the blocking relation.
ISSUE_C = UUID("00000000-0000-7000-8000-0000000000d3")
# Another tenant's, for every isolation assertion.
ISSUE_B = UUID("00000000-0000-7000-8000-0000000000d2")

FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2VlZHNlZWQ$" + "0" * 43


def scope_for(workspace_id: UUID, user_id: UUID) -> AuthorizedWorkspaceScope:
    """A scope as `MembershipService.authorized_scope_for_slug` would build it.

    Constructed by hand here, which is what lets one test build the pair the
    membership lookup would never produce -- workspace B with a user who
    belongs only to A -- and assert that the QUERY refuses it rather than
    relying on the lookup upstream to be the only guard.
    """
    return AuthorizedWorkspaceScope(
        workspace_id=workspace_id,
        user_id=user_id,
        role="member",
    )


@dataclass(frozen=True, slots=True)
class Wired:
    """The real services over one pool, plus a connection to look behind them.

    Built exactly as `get_context` builds them, because the point of this file
    is what happens when application code meets the schema.
    """

    issues: IssueService
    comments: CommentService
    labels: LabelService
    relations: RelationService
    activity: ActivityService
    connection: asyncpg.Connection


async def _seed(connection) -> None:
    await reset_schema(connection)
    await apply_all_migrations(connection)

    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
        WORKSPACE_B,
        "acme",
        "Acme",
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
        TEAM_B,
        WORKSPACE_B,
        "Acme Core",
        "ACME",
    )

    # 005 seeds workflow states for every team that existed when it ran, so
    # the team created above has none and an issue cannot be filed against it.
    await seed_workflow_states(connection, WORKSPACE_B, TEAM_B)

    await connection.executemany(
        "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
        [
            (ALICE, "alice@vector.test", FAKE_HASH),
            (BOB, "bob@vector.test", FAKE_HASH),
            (DAVE, "dave@vector.test", FAKE_HASH),
            (CARA, "cara@vector.test", FAKE_HASH),
            (ERIN, "erin@vector.test", FAKE_HASH),
        ],
    )
    await connection.executemany(
        """
        INSERT INTO workspace_members (workspace_id, user_id, role)
        VALUES ($1, $2, $3)
        """,
        [
            (WORKSPACE_A, ALICE, "owner"),
            (WORKSPACE_A, BOB, "member"),
            (WORKSPACE_A, DAVE, "member"),
            (WORKSPACE_B, CARA, "owner"),
            (WORKSPACE_B, ERIN, "member"),
        ],
    )

    await connection.executemany(
        """
        INSERT INTO issues (
            id, workspace_id, team_id, number, workflow_state_id, title,
            priority, assignee_id, creator_id
        )
        VALUES (
            $1, $2, $3, $4,
            (
                SELECT id FROM workflow_states
                WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
            ),
            $5, 1, $6, $7
        )
        """,
        [
            (ISSUE_A, WORKSPACE_A, TEAM_A, 1, "Assigned to Alice", ALICE, BOB),
            (ISSUE_C, WORKSPACE_A, TEAM_A, 2, "Assigned to Bob", BOB, BOB),
            (ISSUE_B, WORKSPACE_B, TEAM_B, 1, "Issue in B", CARA, CARA),
        ],
    )

    # The counters, moved to match the numbers handed out above. 005 keeps
    # `teams.issue_counter` as the allocator, so a fixture that inserts issues
    # without advancing it leaves the next real create colliding with a number
    # already on the table -- which surfaces as a unique violation in whatever
    # test happens to file the first issue.
    await connection.executemany(
        "UPDATE teams SET issue_counter = $2 WHERE id = $1",
        [(TEAM_A, 2), (TEAM_B, 1)],
    )


@pytest.fixture
async def wired(postgres_dsn):
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await _seed(connection)

        pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=4)

        try:
            yield Wired(
                issues=IssueService(
                    pool=pool,
                    repository=IssueRepository(),
                    teams=TeamService(pool=pool, repository=TeamRepository()),
                ),
                comments=CommentService(pool=pool, repository=CommentRepository()),
                labels=LabelService(
                    pool=pool,
                    repository=LabelRepository(),
                    issue_label_repository=IssueLabelRepository(),
                    group_repository=LabelGroupRepository(),
                ),
                relations=RelationService(
                    pool=pool,
                    repository=RelationRepository(),
                ),
                activity=ActivityService(
                    pool=pool,
                    repository=ActivityRepository(),
                    notifications=NotificationRepository(),
                ),
                connection=connection,
            )
        finally:
            await pool.close()
    finally:
        await connection.close()


async def _activity(connection, issue_id: UUID) -> list[tuple]:
    """One issue's history as (kind, from, to, actor), oldest first.

    Read straight off the connection rather than through the service, so an
    assertion about what was WRITTEN cannot be satisfied by a read path that
    filters or reorders.
    """
    rows = await connection.fetch(
        """
        SELECT kind, from_value, to_value, actor_id
        FROM issue_activity
        WHERE issue_id = $1
        ORDER BY created_at, id
        """,
        issue_id,
    )

    return [tuple(row) for row in rows]


async def _inbox(connection, user_id: UUID) -> list[tuple]:
    """One user's notifications as (kind, issue, actor, read?), oldest first."""
    rows = await connection.fetch(
        """
        SELECT kind, issue_id, actor_id, read_at IS NOT NULL
        FROM notifications
        WHERE user_id = $1
        ORDER BY created_at, id
        """,
        user_id,
    )

    return [tuple(row) for row in rows]


# --------------------------------------------------------------------------
# One transaction, or nothing
# --------------------------------------------------------------------------


async def test_a_failed_history_write_takes_the_change_back_with_it(wired):
    """The claim the whole design rests on, tested from the losing side.

    An actor that names no account violates `issue_activity_actor_fk`, and the
    violation happens AFTER the UPDATE has already changed the title. If the
    history were written on a connection of its own -- or after the fact by an
    event bus -- the title would stay renamed and the timeline would be silent
    about it. Sharing the transaction means the failure reaches back and undoes
    the change.

    Asserted on both halves. The activity table being empty alone would also
    be satisfied by a history that was never attempted.
    """
    ghost = uuid4()

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await wired.issues.update(
            scope=SCOPE_A,
            issue_id=ISSUE_A,
            patch=IssuePatch(title="Renamed"),
            actor_id=ghost,
        )

    assert raised.value.constraint_name == "issue_activity_actor_fk"

    title = await wired.connection.fetchval(
        "SELECT title FROM issues WHERE id = $1", ISSUE_A
    )

    assert title == "Assigned to Alice", "the change must not survive its history"
    assert await _activity(wired.connection, ISSUE_A) == []


async def test_an_update_records_one_row_per_field_that_actually_moved(wired):
    """Two changed fields, one untouched, one rewritten to its own value.

    The last is the interesting one: the UPDATE assigns every column whether
    or not the patch mentioned it, so "the statement wrote it" is not the test
    for "it changed".
    """
    await wired.issues.update(
        scope=SCOPE_A,
        issue_id=ISSUE_A,
        patch=IssuePatch(title="Renamed", priority=3),
        actor_id=BOB,
    )

    assert await _activity(wired.connection, ISSUE_A) == [
        (ActivityKind.TITLE_CHANGED, "Assigned to Alice", "Renamed", BOB),
        (ActivityKind.PRIORITY_CHANGED, "1", "3", BOB),
    ]

    await wired.issues.update(
        scope=SCOPE_A,
        issue_id=ISSUE_A,
        patch=IssuePatch(title="Renamed"),
        actor_id=BOB,
    )

    assert len(await _activity(wired.connection, ISSUE_A)) == 2, (
        "rewriting a field to its own value is not an event"
    )


async def test_archiving_is_recorded_even_though_the_issue_becomes_unreadable(
    wired,
):
    """The one event the row itself can no longer be read to discover.

    Every read on `IssueRepository` carries `archived_at IS NULL`, so after
    this the issue is invisible to the product. Its history is not.
    """
    await wired.issues.archive(scope=SCOPE_A, issue_id=ISSUE_A, actor_id=ALICE)

    assert await _activity(wired.connection, ISSUE_A) == [
        (ActivityKind.ARCHIVED, None, None, ALICE)
    ]


async def test_labels_and_comments_land_in_the_same_history(wired):
    """Three different services writing to one timeline.

    Each records the ID of what it touched rather than a name or a body: a
    label can be renamed and a comment withdrawn, and a history that copied
    either would outlive the thing it copied.
    """
    label = await wired.labels.create(scope=SCOPE_A, name="bug", color=None)

    await wired.labels.attach(
        scope=SCOPE_A, issue_id=ISSUE_A, label_id=label.id, actor_id=DAVE
    )
    await wired.labels.detach(
        scope=SCOPE_A, issue_id=ISSUE_A, label_id=label.id, actor_id=DAVE
    )

    comment = await wired.comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=DAVE, body="looking"
    )

    assert await _activity(wired.connection, ISSUE_A) == [
        (ActivityKind.LABEL_ATTACHED, None, str(label.id), DAVE),
        (ActivityKind.LABEL_DETACHED, None, str(label.id), DAVE),
        (ActivityKind.COMMENTED, None, str(comment.id), DAVE),
    ]


async def test_one_issues_history_never_contains_another_tenants(wired):
    """The workspace is an equality in the read, not a filter afterwards."""
    await wired.issues.update(
        scope=SCOPE_A, issue_id=ISSUE_A, patch=IssuePatch(title="A"), actor_id=ALICE
    )
    await wired.issues.update(
        scope=SCOPE_B, issue_id=ISSUE_B, patch=IssuePatch(title="B"), actor_id=CARA
    )

    page = await wired.activity.list_for_issue(
        scope=SCOPE_A, issue_id=ISSUE_B, first=10, after=None
    )

    assert page.nodes == [], "another tenant's issue answers as one with no history"

    own = await wired.activity.list_for_issue(
        scope=SCOPE_A, issue_id=ISSUE_A, first=10, after=None
    )

    assert [node.to_value for node in own.nodes] == ["A"]


async def test_the_history_pages_newest_first_and_the_walk_is_total(wired):
    """Four events, two pages, no row repeated and none skipped.

    The tie-break matters here more than anywhere: two of these rows are
    written by one statement and share `created_at` to the microsecond, so
    only `id` keeps the ordering total.
    """
    await wired.issues.update(
        scope=SCOPE_A,
        issue_id=ISSUE_A,
        patch=IssuePatch(title="One", priority=2),
        actor_id=ALICE,
    )
    await wired.issues.update(
        scope=SCOPE_A,
        issue_id=ISSUE_A,
        patch=IssuePatch(title="Two", priority=3),
        actor_id=ALICE,
    )

    first = await wired.activity.list_for_issue(
        scope=SCOPE_A, issue_id=ISSUE_A, first=3, after=None
    )
    second = await wired.activity.list_for_issue(
        scope=SCOPE_A, issue_id=ISSUE_A, first=3, after=first.end_cursor
    )

    assert first.has_next_page is True
    assert second.has_next_page is False

    walked = [node.id for node in first.nodes] + [node.id for node in second.nodes]

    assert len(walked) == 4
    assert len(set(walked)) == 4, "a page walk must not repeat a row"


# --------------------------------------------------------------------------
# Nobody is notified of their own action
# --------------------------------------------------------------------------


async def test_assigning_an_issue_to_yourself_notifies_nobody(wired):
    """The rule, at the moment it is most tempting to skip.

    Alice assigns Alice. The recipient and the actor are the same row, so
    there is nobody left to tell -- and a service that filed it anyway would
    be refused by `notifications_actor_is_not_recipient` rather than quietly
    filling an inbox with its owner's own edits.
    """
    await wired.issues.update(
        scope=SCOPE_A,
        issue_id=ISSUE_C,
        patch=IssuePatch(assignee_id=ALICE),
        actor_id=ALICE,
    )

    assert await _inbox(wired.connection, ALICE) == []


async def test_assigning_an_issue_to_somebody_else_notifies_them(wired):
    """The control. Without it the test above passes for a feature that
    notifies nobody at all."""
    await wired.issues.update(
        scope=SCOPE_A,
        issue_id=ISSUE_C,
        patch=IssuePatch(assignee_id=DAVE),
        actor_id=ALICE,
    )

    assert await _inbox(wired.connection, DAVE) == [
        (NotificationKind.ASSIGNED, ISSUE_C, ALICE, False)
    ]
    assert await _inbox(wired.connection, ALICE) == []


async def test_a_comment_reaches_the_assignee_and_the_author_but_not_its_writer(
    wired,
):
    """Three people, three different answers, from one statement.

    Dave writes on an issue assigned to Alice and filed by Bob. Both of them
    hear about it; Dave does not, because he is the actor.
    """
    await wired.comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=DAVE, body="a thought"
    )

    assert await _inbox(wired.connection, ALICE) == [
        (NotificationKind.COMMENTED, ISSUE_A, DAVE, False)
    ]
    assert await _inbox(wired.connection, BOB) == [
        (NotificationKind.COMMENTED, ISSUE_A, DAVE, False)
    ]
    assert await _inbox(wired.connection, DAVE) == []


async def test_commenting_on_your_own_issue_notifies_only_the_other_party(wired):
    """Alice is the assignee and writes the comment.

    One recipient, not two, and not zero: Bob filed the issue and still hears
    about it. The DISTINCT in the insert is what keeps a person who is both
    assignee and author from getting the same event twice -- asserted below.
    """
    await wired.comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=ALICE, body="mine"
    )

    assert await _inbox(wired.connection, ALICE) == []
    assert len(await _inbox(wired.connection, BOB)) == 1


async def test_one_person_who_is_both_assignee_and_author_is_notified_once(wired):
    """ISSUE_C is Bob's own, assigned to Bob. Dave comments on it."""
    await wired.comments.create(
        scope=SCOPE_A, issue_id=ISSUE_C, author_id=DAVE, body="one only"
    )

    assert await _inbox(wired.connection, BOB) == [
        (NotificationKind.COMMENTED, ISSUE_C, DAVE, False)
    ]


async def test_blocking_an_issue_notifies_whoever_is_holding_it(wired):
    """Bob makes his own issue block Alice's, and Alice is the one told.

    Getting the direction wrong would notify the person causing the hold-up
    rather than the person held up, and nothing downstream could detect it:
    both ids name real issues in the same workspace.

    Both ends get a history row, named from their own side, so the target's
    timeline reads `blocked_by` rather than nothing at all.
    """
    await wired.relations.create_relation(
        scope=SCOPE_A,
        source_issue_id=ISSUE_C,
        target_issue_id=ISSUE_A,
        relation_type=RelationType.BLOCKS,
        actor_id=BOB,
    )

    assert await _inbox(wired.connection, ALICE) == [
        (NotificationKind.BLOCKED, ISSUE_A, BOB, False)
    ]
    assert await _inbox(wired.connection, BOB) == []

    assert await _activity(wired.connection, ISSUE_C) == [
        (ActivityKind.RELATION_ADDED, "blocks", str(ISSUE_A), BOB)
    ]
    assert await _activity(wired.connection, ISSUE_A) == [
        (ActivityKind.RELATION_ADDED, "blocked_by", str(ISSUE_C), BOB)
    ]


async def test_the_schema_refuses_a_self_notification_outright(wired):
    """The rule the services keep, kept again where they cannot reach.

    A future writer that computes recipients some other way does not get to
    reintroduce this: the row is not merely unwritten, it is unwritable.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await wired.connection.execute(
            """
            INSERT INTO notifications
                (workspace_id, user_id, actor_id, issue_id, kind)
            VALUES ($1, $2, $2, $3, 'assigned')
            """,
            WORKSPACE_A,
            ALICE,
            ISSUE_A,
        )

    assert raised.value.constraint_name == "notifications_actor_is_not_recipient"


async def test_a_notification_cannot_be_filed_for_a_non_member(wired):
    """The composite key onto `workspace_members`, from the outside.

    Cara is a real account and a member of B. Filing an item about A's issue
    into her inbox would hand her the id, the actor and the timing of work she
    cannot see; there is no `workspace_id` that satisfies both keys.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await wired.connection.execute(
            """
            INSERT INTO notifications
                (workspace_id, user_id, actor_id, issue_id, kind)
            VALUES ($1, $2, $3, $4, 'assigned')
            """,
            WORKSPACE_A,
            CARA,
            ALICE,
            ISSUE_A,
        )

    assert raised.value.constraint_name == "notifications_user_fk"


# --------------------------------------------------------------------------
# The inbox: reading it, and clearing it
# --------------------------------------------------------------------------


async def _notify_alice(wired) -> UUID:
    """One unread item for Alice, and its id."""
    await wired.comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=DAVE, body="something"
    )

    return await wired.connection.fetchval(
        "SELECT id FROM notifications WHERE user_id = $1", ALICE
    )


async def test_marking_read_is_idempotent_and_keeps_the_first_instant(wired):
    """A retry, or a second tab, must not move `readAt`.

    `COALESCE(read_at, now())` is what makes the second call a no-op rather
    than a rewrite, so the timestamp keeps meaning "when this was first read".
    """
    notification_id = await _notify_alice(wired)
    scope = scope_for(WORKSPACE_A, ALICE)

    first = await wired.activity.mark_read(scope=scope, notification_id=notification_id)
    second = await wired.activity.mark_read(
        scope=scope, notification_id=notification_id
    )

    assert first.read_at is not None
    assert second.read_at == first.read_at
    assert await wired.activity.unread_count(scope=scope) == 0


async def test_marking_all_read_counts_what_moved_and_not_what_was_already_read(
    wired,
):
    """Zero the second time, not the same number again.

    The count is what a client uses to zero a badge, so it has to describe
    what this call did rather than what the inbox holds.
    """
    await wired.comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=DAVE, body="one"
    )
    await wired.comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=DAVE, body="two"
    )

    scope = scope_for(WORKSPACE_A, ALICE)

    assert await wired.activity.unread_count(scope=scope) == 2
    assert await wired.activity.mark_all_read(scope=scope) == 2
    assert await wired.activity.mark_all_read(scope=scope) == 0
    assert await wired.activity.unread_count(scope=scope) == 0


async def test_unread_only_and_the_whole_inbox_are_different_lists(wired):
    """Both statements exist because a partial index cannot serve both."""
    notification_id = await _notify_alice(wired)
    scope = scope_for(WORKSPACE_A, ALICE)

    await wired.activity.mark_read(scope=scope, notification_id=notification_id)

    unread = await wired.activity.list_notifications(
        scope=scope, unread_only=True, first=10, after=None
    )
    everything = await wired.activity.list_notifications(
        scope=scope, unread_only=False, first=10, after=None
    )

    assert unread.nodes == []
    assert [node.id for node in everything.nodes] == [notification_id]


async def test_a_member_of_one_workspace_reads_nothing_from_another(wired):
    """The inbox is keyed on (workspace, user) TOGETHER.

    Cara's notification is real and Alice's membership is real; the pair that
    would read it is not, and the query refuses it rather than the lookup
    upstream being the only thing standing in the way.
    """
    await wired.comments.create(
        scope=SCOPE_B, issue_id=ISSUE_B, author_id=ERIN, body="in B"
    )

    assert await _inbox(wired.connection, CARA) != [], "Cara must have something"

    in_own_workspace = await wired.activity.list_notifications(
        scope=scope_for(WORKSPACE_A, ALICE),
        unread_only=False,
        first=10,
        after=None,
    )
    across_the_boundary = await wired.activity.list_notifications(
        # The pair `authorized_scope_for_slug` could never produce: workspace
        # B, with a user who belongs only to A.
        scope=scope_for(WORKSPACE_B, ALICE),
        unread_only=False,
        first=10,
        after=None,
    )

    assert in_own_workspace.nodes == []
    assert across_the_boundary.nodes == []
    assert (await wired.activity.unread_count(scope=scope_for(WORKSPACE_B, ALICE))) == 0


async def test_a_colleague_cannot_mark_your_notification_read(wired):
    """Same workspace, same role, somebody else's inbox.

    Reported as "does not exist" rather than as a refusal: "that is not
    yours" confirms the id names a real item somebody really received. The
    row is asserted still unread, because an error message alone would be
    satisfied by a statement that marked it and then complained.
    """
    notification_id = await _notify_alice(wired)

    with pytest.raises(ValidationError) as raised:
        await wired.activity.mark_read(
            scope=scope_for(WORKSPACE_A, BOB),
            notification_id=notification_id,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("id", "NOT_FOUND")
    ]

    read_at = await wired.connection.fetchval(
        "SELECT read_at FROM notifications WHERE id = $1", notification_id
    )

    assert read_at is None, "the row must still be unread"


async def test_marking_all_read_in_one_workspace_clears_only_that_inbox(wired):
    """Bob's unread items are not Alice's, and the statement says so."""
    await wired.comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=DAVE, body="reaches both"
    )

    assert await wired.activity.mark_all_read(scope=scope_for(WORKSPACE_A, ALICE)) == 1
    assert (await wired.activity.unread_count(scope=scope_for(WORKSPACE_A, BOB))) == 1


# --------------------------------------------------------------------------
# Creating an issue
# --------------------------------------------------------------------------


async def test_filing_an_issue_records_it_and_tells_the_person_it_lands_on(wired):
    """One 'created' row, not six field rows, and one inbox item.

    An issue's initial values are the issue; six changes to something that did
    not exist a moment ago is not a history anybody wants to read.
    """
    entity = await wired.issues.create(
        scope=SCOPE_A,
        team_id=TEAM_A,
        title="New work",
        assignee_id=DAVE,
        creator_id=BOB,
    )

    assert await _activity(wired.connection, entity.id) == [
        (ActivityKind.CREATED, None, "New work", BOB)
    ]
    assert await _inbox(wired.connection, DAVE) == [
        (NotificationKind.ASSIGNED, entity.id, BOB, False)
    ]


async def test_a_rolled_back_creation_leaves_no_history(wired):
    """The atomicity claim from the other direction.

    An assignee who is not a member of this workspace violates
    `issues_assignee_fk`, which the service reports as a field error. The
    'created' row is written in the same transaction, so there is nothing to
    clean up -- and nothing left behind pointing at an issue that does not
    exist.
    """
    before = await wired.connection.fetchval("SELECT count(*) FROM issue_activity")

    with pytest.raises(ValidationError):
        await wired.issues.create(
            scope=SCOPE_A,
            team_id=TEAM_A,
            title="Never filed",
            assignee_id=CARA,
            creator_id=BOB,
        )

    assert await wired.connection.fetchval("SELECT count(*) FROM issue_activity") == (
        before
    )
