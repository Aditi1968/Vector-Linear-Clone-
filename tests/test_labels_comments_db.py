"""Labels and comments against a real PostgreSQL 18, on the whole chain.

Migration 007 makes one claim that no fake connection can check and that no
amount of application code can be trusted to keep: a row joining two
tenant-owned things is *unrepresentable* across tenants. `issue_labels` and
`comments` each carry a single `workspace_id` column and hang two composite
foreign keys off it, so one value has to satisfy both parents at once. An
issue in workspace A wearing a label from workspace B has no such value.

That is worth a server to prove, because the alternative implementations look
identical from Python. Two single-column foreign keys pass every unit test and
admit exactly the cross-tenant row this schema exists to refuse; a
`REFERENCES users (id)` on `comments.author_id` accepts every account in the
installation as the author of a comment in any workspace. So the tests below
attempt the write and assert the *server* refuses it -- never that a service
remembered to check first, which is a property that decays the moment someone
adds a second write path.

Four claims:

  * a label cannot be applied to another tenant's issue, and another tenant's
    label cannot be applied to an issue here;
  * a comment cannot be attributed to a user who is not a member of the
    comment's own workspace;
  * reads are scoped: one workspace's label listing and one issue's comment
    thread never contain another tenant's rows, boundary and tie included;
  * the keyset page walks are total -- no row repeated, none skipped -- over
    a name ordering and a created_at ordering that both contain ties.

Ids come from a suffix scheme whose hex order is its decimal order, so Python
and PostgreSQL sort the seed alike, and expected values are computed in Python
from the seed literals rather than by re-running the query under test.

Marked `db`: deselected by default, skipped when Docker is unreachable.
Nothing here touches DATABASE_URL or Neon; the only server it speaks to is the
throwaway container `postgres_dsn` starts.
"""

from datetime import datetime, timezone
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import ValidationError
from app.domain.tenancy import WorkspaceScope
from app.repositories.comments import CommentRepository
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.label_groups import LabelGroupRepository
from app.repositories.labels import LabelRepository
from app.services.comments import CommentService
from app.services.labels import LabelService

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

# One member of each workspace, and -- the row the author constraint is really
# about -- one account that belongs to NEITHER. 004 seeds no memberships, so
# every one below is inserted by this fixture.
USER_A = UUID("00000000-0000-7000-8000-0000000000c1")
USER_B = UUID("00000000-0000-7000-8000-0000000000c2")
USER_OUTSIDER = UUID("00000000-0000-7000-8000-0000000000c3")

ISSUE_A = UUID("00000000-0000-7000-8000-0000000000d1")
ISSUE_B = UUID("00000000-0000-7000-8000-0000000000d2")

# A password hash that satisfies users_password_hash_argon2id without being
# one: this file authenticates nobody, and a real argon2 hash here would cost
# a KDF per test to prove nothing.
FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2VlZHNlZWQ$" + "0" * 43

# Small on purpose: at four labels per workspace a wrong page is readable at a
# glance, and page size two still makes two pages with a boundary in the
# middle of the seed.
PAGE_SIZE = 2

# Names chosen so their alphabetical order is NOT their insertion order, and
# so that workspace B's names interleave with A's rather than sorting after
# them. A listing that lost its tenant predicate would therefore return a page
# whose first row is right and whose second is somebody else's.
LABELS_A = ("bug", "Chore", "design", "feature")
LABELS_B = ("blocker", "cleanup", "docs", "flaky")

BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _seed(connection) -> None:
    """The whole chain, then two tenants holding rows at once.

    Applied through the real runner rather than by hand-written DDL: the claim
    under test is about the schema the migrations produce, and a second copy of
    that schema in a test file is a copy that can disagree with it.
    """
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
            (USER_A, "a@vector.test", FAKE_HASH),
            (USER_B, "b@vector.test", FAKE_HASH),
            (USER_OUTSIDER, "outsider@vector.test", FAKE_HASH),
        ],
    )
    await connection.executemany(
        """
        INSERT INTO workspace_members (workspace_id, user_id, role)
        VALUES ($1, $2, $3)
        """,
        [
            (WORKSPACE_A, USER_A, "owner"),
            (WORKSPACE_B, USER_B, "owner"),
        ],
    )

    # One issue per tenant, seeded directly: these rows exist to be written
    # against, and their numbers and states are whatever the schema demands
    # rather than anything this file asserts on.
    await connection.executemany(
        """
        INSERT INTO issues (
            id, workspace_id, team_id, number, workflow_state_id, title, priority
        )
        VALUES (
            $1, $2, $3, 1,
            (
                SELECT id FROM workflow_states
                WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
            ),
            $4, 1
        )
        """,
        [
            (ISSUE_A, WORKSPACE_A, TEAM_A, "Issue in A"),
            (ISSUE_B, WORKSPACE_B, TEAM_B, "Issue in B"),
        ],
    )


@pytest.fixture
async def wired(postgres_dsn):
    """A migrated database, two tenants, and the real service objects.

    The services are built over a pool exactly as `get_context` builds them,
    because the point of this file is what happens when application code meets
    the schema -- a repository driven directly would skip the transaction
    boundaries and the constraint translation that are half the behaviour.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await _seed(connection)

        pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

        try:
            yield (
                LabelService(
                    pool=pool,
                    repository=LabelRepository(),
                    issue_label_repository=IssueLabelRepository(),
                    group_repository=LabelGroupRepository(),
                ),
                CommentService(pool=pool, repository=CommentRepository()),
                connection,
            )
        finally:
            await pool.close()
    finally:
        await connection.close()


# --------------------------------------------------------------------------
# The composite foreign keys
# --------------------------------------------------------------------------


async def test_a_label_cannot_be_applied_to_another_tenants_issue(wired):
    """The join's whole reason for carrying one workspace_id column.

    Workspace A's scope, A's label, B's issue. Both ids are real; neither is
    guessed. Two single-column foreign keys would each be satisfied -- the
    issue exists, the label exists -- and the row would be written, putting A's
    taxonomy onto B's work.

    Reported as "Issue does not exist" rather than as a tenancy error, and the
    wording is the security property rather than politeness: "that issue is in
    another workspace" confirms the id names something real.
    """
    labels, _, _ = wired

    label = await labels.create(scope=SCOPE_A, name="bug", color=None)

    with pytest.raises(ValidationError) as raised:
        await labels.attach(scope=SCOPE_A, issue_id=ISSUE_B, label_id=label.id)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("issueId", "NOT_FOUND")
    ]


async def test_another_tenants_label_cannot_be_applied_here(wired):
    """The mirror image, which fails on the other foreign key.

    Same workspace, same statement, and the failure has to come from
    `issue_labels_label_fk` this time. A schema that pinned only the issue
    would pass the test above and fail this one, which is why both directions
    are asserted rather than one standing in for the pair.
    """
    labels, _, _ = wired

    foreign = await labels.create(scope=SCOPE_B, name="blocker", color=None)

    with pytest.raises(ValidationError) as raised:
        await labels.attach(scope=SCOPE_A, issue_id=ISSUE_A, label_id=foreign.id)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("labelId", "NOT_FOUND")
    ]


async def test_a_label_applies_to_an_issue_in_its_own_workspace(wired):
    """The control. Without it the two refusals above are satisfied by a
    schema that refuses everything."""
    labels, _, connection = wired

    label = await labels.create(scope=SCOPE_A, name="bug", color=None)

    await labels.attach(scope=SCOPE_A, issue_id=ISSUE_A, label_id=label.id)

    assert (
        await connection.fetchval(
            """
        SELECT count(*) FROM issue_labels
        WHERE workspace_id = $1 AND issue_id = $2 AND label_id = $3
        """,
            WORKSPACE_A,
            ISSUE_A,
            label.id,
        )
        == 1
    )


async def test_a_comment_cannot_be_attributed_to_a_non_member(wired):
    """The constraint that a `REFERENCES users (id)` would not have made.

    USER_OUTSIDER is a real account, USER_B is a real account in a real
    workspace, and neither is a member of workspace A. Under a single-column
    author key both would be accepted as the author of a comment on A's issue,
    with nothing but application discipline standing between the API and
    attributing words to whoever a caller named.

    Both are answered "Issue does not exist", which is the same answer an
    unknown issue id gets. Someone who does not belong to this workspace may
    not read its issues either, so that is the only true thing the server can
    say to them -- and it means which of the two constraints the planner
    checked first is unobservable.
    """
    _, comments, _ = wired

    for stranger in (USER_OUTSIDER, USER_B):
        with pytest.raises(ValidationError) as raised:
            await comments.create(
                scope=SCOPE_A,
                issue_id=ISSUE_A,
                author_id=stranger,
                body="I should not be able to say this here.",
            )

        assert [(issue.field, issue.code) for issue in raised.value.issues] == [
            ("issueId", "NOT_FOUND")
        ]


async def test_a_member_can_comment_in_their_own_workspace(wired):
    """The control for the author key, and the shape of a fresh comment."""
    _, comments, _ = wired

    entity = await comments.create(
        scope=SCOPE_A,
        issue_id=ISSUE_A,
        author_id=USER_A,
        body="Reproduced on main.",
    )

    assert entity.issue_id == ISSUE_A
    assert entity.author_id == USER_A

    # NULL, not created_at: a comment nobody has edited has no edit to report,
    # and seeding the column would make "has this been edited" unanswerable.
    assert entity.edited_at is None


async def test_a_comment_cannot_be_written_onto_another_tenants_issue(wired):
    """A's member, A's scope, B's issue -- refused by `comments_issue_fk`."""
    _, comments, _ = wired

    with pytest.raises(ValidationError) as raised:
        await comments.create(
            scope=SCOPE_A,
            issue_id=ISSUE_B,
            author_id=USER_A,
            body="Wrong tenant.",
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("issueId", "NOT_FOUND")
    ]


async def test_only_the_author_can_delete_their_comment(wired):
    """Authorship is a column in the WHERE clause, not a check after a read.

    A comment written by somebody else answers exactly as one that never
    existed. An error saying "that is not yours" would confirm that the id
    names a real comment a real person really wrote.
    """
    _, comments, connection = wired

    # USER_B joins workspace A as well, so that "another author" here is a
    # fellow MEMBER rather than a stranger -- the case a tenant predicate alone
    # would happily let through.
    await connection.execute(
        """
        INSERT INTO workspace_members (workspace_id, user_id, role)
        VALUES ($1, $2, $3)
        """,
        WORKSPACE_A,
        USER_B,
        "member",
    )

    mine = await comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=USER_A, body="Mine."
    )
    theirs = await comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=USER_B, body="Theirs."
    )

    with pytest.raises(ValidationError) as raised:
        await comments.delete(scope=SCOPE_A, comment_id=theirs.id, author_id=USER_A)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("id", "NOT_FOUND")
    ]

    # The refused delete removed nothing, and the permitted one removes
    # exactly its own row.
    assert (
        await comments.delete(scope=SCOPE_A, comment_id=mine.id, author_id=USER_A)
        == mine.id
    )

    remaining = await connection.fetch(
        "SELECT id FROM comments WHERE workspace_id = $1", WORKSPACE_A
    )

    assert [row["id"] for row in remaining] == [theirs.id]


async def test_removing_a_member_who_has_commented_is_refused(wired):
    """`comments_author_fk ON DELETE RESTRICT`, stated as the consequence it is.

    This is the price of the composite author key and it is deliberate: a
    workspace cannot drop a member whose words are still in its discussions
    until someone decides what happens to them. The alternative, CASCADE,
    destroys a discussion as a side effect of an administrative removal.

    Asserted here so that the day someone writes the "remove a member" path,
    the constraint they will meet is already written down.
    """
    _, comments, connection = wired

    await comments.create(
        scope=SCOPE_A, issue_id=ISSUE_A, author_id=USER_A, body="On the record."
    )

    # RestrictViolationError and not ForeignKeyViolationError, which is worth
    # pinning rather than widening to a base class. An explicit `ON DELETE
    # RESTRICT` raises this; the SQL default, NO ACTION, raises the other. So
    # this assertion also proves the clause was written, and would fail if the
    # constraint were ever relaxed to the default it superficially resembles.
    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            WORKSPACE_A,
            USER_A,
        )


async def test_deleting_a_label_detaches_it_without_touching_the_issue(wired):
    """`issue_labels_label_fk ON DELETE CASCADE` -- and no further.

    Deleting a label is how a workspace stops using one, so RESTRICT here
    would make a label undeletable for exactly as long as anything wore it.
    The association goes; the issue does not.
    """
    labels, _, connection = wired

    label = await labels.create(scope=SCOPE_A, name="bug", color=None)
    await labels.attach(scope=SCOPE_A, issue_id=ISSUE_A, label_id=label.id)

    await labels.delete(scope=SCOPE_A, label_id=label.id)

    assert await connection.fetchval("SELECT count(*) FROM issue_labels") == 0
    assert (
        await connection.fetchval("SELECT count(*) FROM issues WHERE id = $1", ISSUE_A)
        == 1
    )


# --------------------------------------------------------------------------
# Scoped reads and total page walks
# --------------------------------------------------------------------------


async def _seed_labels(labels: LabelService) -> None:
    for name in LABELS_A:
        await labels.create(scope=SCOPE_A, name=name, color=None)

    for name in LABELS_B:
        await labels.create(scope=SCOPE_B, name=name, color=None)


async def _walk(labels: LabelService, scope: WorkspaceScope) -> list[str]:
    """Every label the caller can reach, one page at a time.

    Read through the cursor rather than in one page, because the claim is
    about the walk: a keyset comparison that disagreed with the ORDER BY would
    still return a correct FIRST page.
    """
    names: list[str] = []
    after: str | None = None

    while True:
        page = await labels.list(scope=scope, first=PAGE_SIZE, after=after)

        names.extend(entity.name for entity in page.nodes)

        if not page.has_next_page:
            return names

        after = page.end_cursor


async def test_a_label_walk_stays_inside_one_workspace(wired):
    """Every page, not just the first, and both tenants asked separately.

    The seed interleaves the two workspaces' names alphabetically, so a
    listing that lost its tenant predicate returns a page whose first row is
    right and whose second belongs to someone else -- which is what makes the
    whole walk the assertion rather than its length.
    """
    labels, _, _ = wired

    await _seed_labels(labels)

    # PostgreSQL's default collation, not Python's: 'Chore' sorts among the
    # lowercase names under en_US-style rules and before them under C. Sorting
    # the expectation in Python would be a second ordering that can disagree
    # with the one the index produced, so the claim is set membership plus
    # "sorted the way the server sorted it".
    walked_a = await _walk(labels, SCOPE_A)
    walked_b = await _walk(labels, SCOPE_B)

    assert set(walked_a) == set(LABELS_A)
    assert set(walked_b) == set(LABELS_B)

    # No row repeated and none skipped: the walk is exactly as long as the
    # seed, which a cursor that re-read its own boundary row would not be.
    assert len(walked_a) == len(LABELS_A)
    assert len(walked_b) == len(LABELS_B)


async def test_a_cursor_minted_in_one_workspace_selects_nothing_in_another(wired):
    """A cursor is Base64 over JSON -- readable and writable by whoever holds
    one -- so it carries no tenant and could not be trusted if it did. Replayed
    against another workspace it resumes in THAT workspace's ordering, over
    that workspace's rows, and never in the data it was minted from.
    """
    labels, _, _ = wired

    await _seed_labels(labels)

    first_a = await labels.list(scope=SCOPE_A, first=PAGE_SIZE, after=None)

    resumed = await labels.list(
        scope=SCOPE_B, first=PAGE_SIZE, after=first_a.end_cursor
    )

    assert set(LABELS_B).issuperset(entity.name for entity in resumed.nodes)


async def test_a_comment_thread_is_scoped_to_its_issue_and_its_tenant(wired):
    """Two issues in two workspaces, commented on by their own members.

    The equality on `issue_id` alone would be enough to separate these two
    threads today; the equality on `workspace_id` is what keeps that true when
    two workspaces hold ids that collide, and what makes reading another
    tenant's thread return an empty page rather than an error that reports the
    issue is real.
    """
    _, comments, _ = wired

    for index in range(3):
        await comments.create(
            scope=SCOPE_A, issue_id=ISSUE_A, author_id=USER_A, body=f"A{index}"
        )
        await comments.create(
            scope=SCOPE_B, issue_id=ISSUE_B, author_id=USER_B, body=f"B{index}"
        )

    page = await comments.list_for_issue(
        scope=SCOPE_A, issue_id=ISSUE_A, first=10, after=None
    )

    assert [node.body for node in page.nodes] == ["A0", "A1", "A2"]

    # Another tenant's issue: an empty page, exactly as an issue with no
    # comments produces. Not an error, which would report that it exists.
    foreign = await comments.list_for_issue(
        scope=SCOPE_A, issue_id=ISSUE_B, first=10, after=None
    )

    assert foreign.nodes == []
    assert foreign.has_next_page is False
    assert foreign.end_cursor is None


async def test_a_comment_walk_is_total_over_a_created_at_tie(wired):
    """The keyset has to break a `created_at` tie or the walk is not total.

    `now()` is the transaction's start time, so comments written in one
    statement share a timestamp exactly. The seed forces that: without `id` in
    the comparison a page boundary landing inside the tie either repeats the
    boundary row forever or steps over the rest of it.
    """
    _, comments, connection = wired

    tied = [
        (UUID(f"00000000-0000-7000-8000-0000000f000{index}"), f"tied {index}")
        for index in range(5)
    ]

    await connection.executemany(
        """
        INSERT INTO comments (id, workspace_id, issue_id, author_id, body, created_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        [
            (comment_id, WORKSPACE_A, ISSUE_A, USER_A, body, BASE_TIME)
            for comment_id, body in tied
        ],
    )

    walked: list[str] = []
    after: str | None = None

    while True:
        page = await comments.list_for_issue(
            scope=SCOPE_A, issue_id=ISSUE_A, first=PAGE_SIZE, after=after
        )

        walked.extend(node.body for node in page.nodes)

        if not page.has_next_page:
            break

        after = page.end_cursor

    assert walked == [body for _, body in tied]
