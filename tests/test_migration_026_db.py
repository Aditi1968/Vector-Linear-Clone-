"""Migration 026 applied by the real runner, then asked what it built.

026 adds one nullable column, which is not much of a schema change and is
entirely a change of what removing a member means. The reason it exists is a
fact about the seventeen foreign keys already pointing at `workspace_members`:
seven of them are authorship -- a comment, a project update, a document
revision -- every one ON DELETE RESTRICT, and every one in a migration that is
applied and therefore immutable. Between them they make the DELETE that used to
end a membership impossible for anybody who has ever said anything, and there is
no version of "reassign it first" that applies, because a comment cannot be
reassigned to somebody who did not write it.

So the assertions here are about the shape of the escape, and they are all
facts a fake connection cannot state:

  * the column is nullable with no default, which is what makes every
    membership that existed before this migration ran an active one and is why
    no backfill statement was needed;
  * `workspace_members_removed_after_created` refuses a membership that ended
    before it began;
  * a stamped membership still satisfies `comments_author_fk` -- the comment is
    untouched and still names its author, which is the entire point;
  * deleting that same row is STILL refused, by that same constraint. This is
    the assertion that says the tombstone is the only available move rather
    than a nicety: nothing 026 does relaxes the RESTRICT, and it does not try;
  * an issue assigned to a stamped member keeps its `assignee_id`. 006 wrote
    `ON DELETE SET NULL (assignee_id)` and that clause is now unreachable, so
    the unassignment it used to perform is the service's job -- see
    `MembershipRepository.unassign_issues`.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

LEAVER_ID = UUID("00000000-0000-7000-8000-0000000000e1")

ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000b1")

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
    assignee_id
)
VALUES (
    $1, $2, $3, 1,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
    ),
    'Something they were doing', 1, $4
)
"""


@pytest.fixture
async def connection(postgres_dsn):
    """A migrated database with one member who has left a mark on things."""
    conn = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(conn)
        await apply_all_migrations(conn)
        await seed(conn)

        yield conn
    finally:
        await reset_schema(conn)
        await conn.close()


async def seed(connection) -> None:
    """One member of the bootstrap workspace, holding one of each thing.

    A comment, because that is the authorship key nothing can reassign, and an
    assigned issue, because that is the one key of the seventeen that was
    already ON DELETE SET NULL and whose behaviour therefore changes rather
    than merely being preserved.
    """
    await connection.execute(INSERT_USER_SQL, LEAVER_ID)

    await connection.execute(
        "INSERT INTO workspace_members (workspace_id, user_id, role) "
        "VALUES ($1, $2, 'member')",
        BOOTSTRAP_WORKSPACE_ID,
        LEAVER_ID,
    )

    await connection.execute(
        INSERT_ISSUE_SQL,
        ISSUE_ID,
        BOOTSTRAP_WORKSPACE_ID,
        BOOTSTRAP_TEAM_ID,
        LEAVER_ID,
    )

    await connection.execute(
        "INSERT INTO comments (workspace_id, issue_id, author_id, body) "
        "VALUES ($1, $2, $3, 'I looked at this and it is the cache')",
        BOOTSTRAP_WORKSPACE_ID,
        ISSUE_ID,
        LEAVER_ID,
    )


async def mark_removed(connection) -> None:
    await connection.execute(
        "UPDATE workspace_members SET removed_at = now() "
        "WHERE workspace_id = $1 AND user_id = $2",
        BOOTSTRAP_WORKSPACE_ID,
        LEAVER_ID,
    )


async def test_the_column_is_nullable_with_no_default(connection):
    """Which is what makes every membership that predates 026 an active one.

    Both halves matter and they say different things. Nullable is what lets the
    column mean "has not happened"; no default is what stopped this migration
    needing a backfill statement, and -- 004's argument about `role`, applied
    to the other end of a membership's life -- what stops an INSERT that
    forgets the column from silently creating a membership that has already
    ended.
    """
    column = await connection.fetchrow(
        """
        SELECT data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = 'workspace_members' AND column_name = 'removed_at'
        """
    )

    assert column is not None, "026 did not add workspace_members.removed_at"

    assert (
        column["data_type"],
        column["is_nullable"],
        column["column_default"],
    ) == ("timestamp with time zone", "YES", None)

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM workspace_members WHERE removed_at IS NULL"
        )
        > 0
    ), "the memberships that existed before 026 must read as active"


async def test_a_membership_cannot_end_before_it_began(connection):
    """`workspace_members_removed_after_created`, and why it is not fussiness.

    A row whose two timestamps contradict each other is not an unusual
    membership, it is the output of a bug -- a stamp built in the application
    from a clock that was not the server's, most likely -- and every read that
    orders or reports departures would be quietly wrong about it.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            "UPDATE workspace_members SET removed_at = created_at - interval '1 day' "
            "WHERE workspace_id = $1 AND user_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            LEAVER_ID,
        )

    assert raised.value.constraint_name == "workspace_members_removed_after_created"


async def test_a_stamped_membership_still_owns_its_comment(connection):
    """The whole reason for the column, in one assertion.

    `comments_author_fk` is composite through `workspace_id` and points at this
    membership. Stamping the row leaves the referent in place, so the comment is
    not merely undeleted -- it still resolves, and a reader of that issue is
    still told who wrote it.
    """
    await mark_removed(connection)

    author = await connection.fetchrow(
        """
        SELECT comments.author_id, comments.body, member.removed_at
        FROM comments
        JOIN workspace_members AS member
            ON member.workspace_id = comments.workspace_id
            AND member.user_id = comments.author_id
        WHERE comments.workspace_id = $1
        """,
        BOOTSTRAP_WORKSPACE_ID,
    )

    assert author is not None, "the comment no longer resolves to a membership"
    assert author["author_id"] == LEAVER_ID
    assert author["removed_at"] is not None


async def test_deleting_that_membership_is_still_refused(connection):
    """026 relaxes no constraint, and this is the test that says so.

    It would be an easy misreading of this migration that removal is now
    "soft" and the hard version is still available underneath. It is not: the
    DELETE is exactly as impossible as it was before, which is why the column
    had to exist. A later migration that adds a purge path has to answer
    `comments_author_fk` first, and this assertion is what it will fail on.
    """
    await mark_removed(connection)

    # RestrictViolationError specifically, and not the ForeignKeyViolationError
    # it reads like a kind of: asyncpg derives both from
    # IntegrityConstraintViolationError and neither from the other, so the
    # broader-looking spelling does not catch this at all. The distinction is
    # the same one `remove_member` used to depend on.
    with pytest.raises(asyncpg.RestrictViolationError) as raised:
        await connection.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            LEAVER_ID,
        )

    assert raised.value.constraint_name == "comments_author_fk"


async def test_stamping_does_not_unassign_the_issue(connection):
    """Which is why `unassign_issues` exists in the repository.

    `issues_assignee_fk` is the one key of the seventeen that was already
    `ON DELETE SET NULL (assignee_id)`, so while removal was a DELETE the
    database vacated assignments by itself. An UPDATE fires no ON DELETE
    clause, so that behaviour is now the service's to perform -- and if it ever
    stops performing it, work stays assigned to somebody who left, which this
    assertion is the schema-level explanation of.
    """
    await mark_removed(connection)

    assert (
        await connection.fetchval(
            "SELECT assignee_id FROM issues WHERE id = $1", ISSUE_ID
        )
        == LEAVER_ID
    )
