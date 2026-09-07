"""Onboarding, membership and invitations against a real PostgreSQL 18.

Every claim in this file is one that a fake connection cannot make. A
FakeConnection agrees with whatever SQL it is handed, so it can show that a
service *sends* an UPDATE with `accepted_at IS NULL` in the predicate; only a
real server can show that two callers presenting the same token at the same
moment do not both get a membership out of it.

The five things under test, and why each needs a server:

  * creating a workspace grants the creator ownership -- two INSERTs into two
    tables in one transaction, so the evidence is a row in the second table
    after the first one committed.
  * creating a team seeds a board -- and seeds the *same* board migration 005
    gave the bootstrap team, which is checked by comparing against that team
    rather than against a constant this file also holds.
  * an invitation is single-use under concurrency -- the whole point of the
    UPDATE ... WHERE accepted_at IS NULL, and unobservable without two
    connections racing.
  * an expired invitation is refused -- expiry is `now()` on the server, so
    the row has to exist with a past timestamp.
  * a workspace never loses its last owner, and a member of one workspace
    cannot read another's people or invitations.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import asyncio
import hashlib
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import ValidationError, WorkspaceAccessDeniedError
from app.repositories.invitations import InvitationRepository
from app.repositories.memberships import SHARED_HOLDINGS, MembershipRepository
from app.repositories.teams import TeamRepository
from app.repositories.workspaces import WorkspaceRepository
from app.services.memberships import _REMOVAL_BLOCKED, MembershipService
from app.services.teams import TeamService

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

# The team migration 002 seeds and 005 gives a board to. This file compares a
# team the application creates against it, which is what pins
# DEFAULT_WORKFLOW_STATES equal to the migration's own seed -- a constant
# compared against a copy of itself would prove nothing.
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

FOUNDER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
COLLEAGUE_ID = UUID("00000000-0000-7000-8000-0000000000e2")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e3")

INSERT_USER_SQL = """
INSERT INTO users (id, email, password_hash)
VALUES (
    $1::uuid,
    'user-' || $1::text || '@example.test',
    '$argon2id$not-a-real-hash'
)
"""

# An invitation the application could not have written: already expired, and
# created before it expired so that
# `workspace_invitations_expiry_after_creation` still holds. Written by hand
# because `create_invitation` always issues a live one -- which is the point,
# and also why the expired case needs a fixture of its own.
INSERT_EXPIRED_INVITATION_SQL = """
INSERT INTO workspace_invitations (
    workspace_id, email, role, token_hash, expires_at, created_at
)
VALUES ($1, $2, 'member', $3, now() - interval '1 day', now() - interval '8 days')
"""


@pytest.fixture
async def pool(postgres_dsn):
    """A migrated database with three accounts and no workspaces of their own.

    The accounts exist because `workspace_members_user_fk` requires them; no
    memberships are seeded, because every membership this file cares about is
    one the application is supposed to create. `max_size` is above one so that
    the concurrency test can actually run two transactions at once.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)

        for user_id in (FOUNDER_ID, COLLEAGUE_ID, OUTSIDER_ID):
            await connection.execute(INSERT_USER_SQL, user_id)
    finally:
        await connection.close()

    created = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=4)

    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
def memberships(pool) -> MembershipService:
    """The real service over the real repositories over the real database."""
    return MembershipService(
        pool=pool,
        repository=MembershipRepository(),
        workspaces=WorkspaceRepository(),
        invitations=InvitationRepository(),
    )


@pytest.fixture
def teams(pool) -> TeamService:
    return TeamService(pool=pool, repository=TeamRepository())


async def acme(memberships: MembershipService):
    """A workspace owned by the founder, made the way the product makes one."""
    return await memberships.create_workspace(
        name="Acme",
        slug="acme",
        owner_id=FOUNDER_ID,
    )


async def owner_scope(memberships: MembershipService):
    return await memberships.authorized_scope_for_slug(
        slug="acme",
        user_id=FOUNDER_ID,
    )


async def join_as(memberships: MembershipService, *, user_id: UUID, role: str):
    """Put someone in acme through the invitation path, not through SQL.

    Every test that needs a second person needs them to have arrived the way a
    real one does, so a defect in the invitation path fails those tests too
    rather than being papered over by a seeded row.
    """
    scope = await owner_scope(memberships)

    _, token = await memberships.create_invitation(
        scope=scope,
        email=f"invitee-{user_id}@example.test",
        role=role,
    )

    return await memberships.accept_invitation(token=token, user_id=user_id)


# ------------------------------------------------------- creating a workspace


async def test_creating_a_workspace_grants_the_creator_ownership(memberships, pool):
    """The membership is the point of the mutation, not a follow-up call.

    A workspace whose creator is not a member of it is a tenant nobody can
    enter -- not even by invitation, which requires being an admin of it
    already -- so the row in the second table is what makes the row in the
    first one worth anything.
    """
    membership = await acme(memberships)

    assert (membership.workspace_slug, membership.role) == ("acme", "owner")

    stored = await pool.fetchval(
        """
        SELECT role
        FROM workspace_members
        JOIN workspaces ON workspaces.id = workspace_members.workspace_id
        WHERE workspaces.slug = $1 AND workspace_members.user_id = $2
        """,
        "acme",
        FOUNDER_ID,
    )

    assert stored == "owner"


async def test_a_taken_slug_is_a_field_error_and_leaves_one_workspace(
    memberships, pool
):
    """`workspaces_slug_key` must reach the client as something it can fix.

    A raw UniqueViolationError would arrive as a masked "Internal server
    error", which a signup form cannot render and a person cannot act on. The
    second half of the assertion matters as much: the failed attempt must not
    have left a workspace behind for the transaction that failed after it.
    """
    await acme(memberships)

    with pytest.raises(ValidationError) as raised:
        await memberships.create_workspace(
            name="Acme Rival",
            slug="acme",
            owner_id=COLLEAGUE_ID,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("slug", "SLUG_TAKEN")
    ]

    assert (
        await pool.fetchval("SELECT count(*) FROM workspaces WHERE slug = $1", "acme")
        == 1
    )


# ----------------------------------------------------------- creating a team


async def test_creating_a_team_seeds_the_board_migration_005_seeds(
    memberships, teams, pool
):
    """A team without workflow states cannot hold an issue at all.

    Compared against the bootstrap team's own board rather than against a
    literal list, because the claim is not "these five names" -- it is that
    `DEFAULT_WORKFLOW_STATES` and the seed in 005 have not drifted apart. A
    literal here would be a third copy that can agree with neither.
    """
    await acme(memberships)

    workflow = await teams.create(
        scope=await owner_scope(memberships),
        name="Engineering",
        key="ENG",
    )

    seeded = await pool.fetch(
        """
        SELECT name, type, position, color
        FROM workflow_states
        WHERE team_id = $1
        ORDER BY position, id
        """,
        BOOTSTRAP_TEAM_ID,
    )

    assert [
        (state.name, state.category.value, state.position, state.color)
        for state in workflow.workflow_states
    ] == [(row["name"], row["type"], row["position"], row["color"]) for row in seeded]


async def test_a_team_key_taken_in_the_workspace_is_a_field_error(memberships, teams):
    """`teams_workspace_key_unique`, reported as something a form can show."""
    await acme(memberships)

    scope = await owner_scope(memberships)

    await teams.create(scope=scope, name="Engineering", key="ENG")

    with pytest.raises(ValidationError) as raised:
        await teams.create(scope=scope, name="Engine Room", key="ENG")

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("key", "KEY_TAKEN")
    ]


async def test_an_ordinary_member_cannot_create_a_team(memberships, teams):
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    scope = await memberships.authorized_scope_for_slug(
        slug="acme",
        user_id=COLLEAGUE_ID,
    )

    with pytest.raises(WorkspaceAccessDeniedError):
        await teams.create(scope=scope, name="Sales", key="SALES")


# ------------------------------------------------------------- invitations


async def test_only_the_hash_of_a_token_is_stored(memberships, pool):
    """The column must hold a digest, not the credential.

    A row holding the token itself would turn any read of this table -- a
    backup, a support query, a log line -- into a grant of access to every
    tenant with an open invitation.
    """
    await acme(memberships)

    invitation, token = await memberships.create_invitation(
        scope=await owner_scope(memberships),
        email="new@example.test",
        role="member",
    )

    stored = await pool.fetchval(
        "SELECT token_hash FROM workspace_invitations WHERE id = $1",
        invitation.id,
    )

    assert stored == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in stored


async def test_an_invitation_can_be_accepted_only_once(memberships):
    """The second redemption of a spent token is refused, and says no more.

    The refusal must not distinguish "already accepted" from "never existed":
    a client that could tell them apart could ask whether a guessed string was
    ever a real invitation here.
    """
    await acme(memberships)

    _, token = await memberships.create_invitation(
        scope=await owner_scope(memberships),
        email="new@example.test",
        role="member",
    )

    joined = await memberships.accept_invitation(token=token, user_id=COLLEAGUE_ID)

    assert (joined.workspace_slug, joined.role) == ("acme", "member")

    with pytest.raises(ValidationError) as raised:
        await memberships.accept_invitation(token=token, user_id=OUTSIDER_ID)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("token", "INVALID")
    ]


async def test_two_simultaneous_redemptions_produce_exactly_one_membership(
    memberships, pool
):
    """The single-use guarantee, under the concurrency it exists for.

    `SELECT` then `UPDATE` passes every test above and fails this one: both
    callers see `accepted_at IS NULL`, both proceed, and both get a
    membership. One statement that marks and returns under a row lock is what
    makes the loser of the race receive nothing.
    """
    await acme(memberships)

    _, token = await memberships.create_invitation(
        scope=await owner_scope(memberships),
        email="new@example.test",
        role="member",
    )

    outcomes = await asyncio.gather(
        memberships.accept_invitation(token=token, user_id=COLLEAGUE_ID),
        memberships.accept_invitation(token=token, user_id=OUTSIDER_ID),
        return_exceptions=True,
    )

    refused = [outcome for outcome in outcomes if isinstance(outcome, ValidationError)]

    assert len(refused) == 1, f"expected exactly one refusal, got {outcomes}"

    assert (
        await pool.fetchval(
            """
            SELECT count(*)
            FROM workspace_members
            JOIN workspaces ON workspaces.id = workspace_members.workspace_id
            WHERE workspaces.slug = $1 AND workspace_members.user_id <> $2
            """,
            "acme",
            FOUNDER_ID,
        )
        == 1
    )


async def test_an_expired_invitation_is_refused_and_grants_nothing(memberships, pool):
    """Expiry is decided by the server's clock, in the same statement as the
    claim.

    The membership count is asserted as well as the refusal, because a
    redemption path that raised after inserting would fail the first
    assertion and pass nothing useful.
    """
    membership = await acme(memberships)

    token = "an-expired-invitation-token"

    await pool.execute(
        INSERT_EXPIRED_INVITATION_SQL,
        membership.workspace_id,
        "late@example.test",
        hashlib.sha256(token.encode("utf-8")).hexdigest(),
    )

    with pytest.raises(ValidationError) as raised:
        await memberships.accept_invitation(token=token, user_id=COLLEAGUE_ID)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("token", "INVALID")
    ]

    assert (
        await pool.fetchval(
            "SELECT count(*) FROM workspace_members WHERE user_id = $1",
            COLLEAGUE_ID,
        )
        == 0
    )


async def test_an_expired_invitation_is_not_listed_as_pending(memberships, pool):
    """What the admins' list shows and what `claim` will accept must agree."""
    membership = await acme(memberships)

    await pool.execute(
        INSERT_EXPIRED_INVITATION_SQL,
        membership.workspace_id,
        "late@example.test",
        hashlib.sha256(b"another-expired-token").hexdigest(),
    )

    pending = await memberships.list_invitations(scope=await owner_scope(memberships))

    assert pending == []


# ------------------------------------------------------------- tenant reach


async def test_a_member_of_one_workspace_cannot_reach_another(memberships):
    """Reading another tenant's people is refused before there is a scope.

    `list_members` and `list_invitations` take an AuthorizedWorkspaceScope,
    which can only be built from a row in `workspace_members` -- so the check
    this test performs is the only way in, and it fails for a workspace the
    caller is not in. The refusal is the same one a nonexistent slug gets.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    await memberships.create_workspace(
        name="Globex",
        slug="globex",
        owner_id=OUTSIDER_ID,
    )

    with pytest.raises(WorkspaceAccessDeniedError):
        await memberships.authorized_scope_for_slug(
            slug="globex",
            user_id=COLLEAGUE_ID,
        )


async def test_a_member_sees_the_people_but_not_the_invitations(memberships):
    """Members read the member list; only admins read pending invitations.

    A pending invitation discloses an address belonging to someone who has not
    joined and may never, which is not the same disclosure as the colleague
    list every assignee picker needs.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    scope = await memberships.authorized_scope_for_slug(
        slug="acme",
        user_id=COLLEAGUE_ID,
    )

    members = await memberships.list_members(scope=scope)

    assert {member.user_id for member in members} == {FOUNDER_ID, COLLEAGUE_ID}

    with pytest.raises(WorkspaceAccessDeniedError):
        await memberships.list_invitations(scope=scope)


async def test_an_admin_cannot_mint_an_owner(memberships):
    """Otherwise an admin promotes itself by way of an address it controls."""
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="admin")

    scope = await memberships.authorized_scope_for_slug(
        slug="acme",
        user_id=COLLEAGUE_ID,
    )

    with pytest.raises(WorkspaceAccessDeniedError):
        await memberships.create_invitation(
            scope=scope,
            email="puppet@example.test",
            role="owner",
        )

    with pytest.raises(WorkspaceAccessDeniedError):
        await memberships.update_member_role(
            scope=scope,
            user_id=COLLEAGUE_ID,
            role="owner",
        )


# --------------------------------------------------------------- last owner


async def test_the_last_owner_cannot_be_removed_or_demoted(memberships):
    """A workspace with no owner is one nobody can ever administer again.

    Both paths are checked, because the rule lives in two methods and a fix
    applied to one of them is the shape this failure takes.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="admin")

    scope = await owner_scope(memberships)

    with pytest.raises(ValidationError) as removal:
        await memberships.remove_member(scope=scope, user_id=FOUNDER_ID)

    with pytest.raises(ValidationError) as demotion:
        await memberships.update_member_role(
            scope=scope,
            user_id=FOUNDER_ID,
            role="member",
        )

    assert [issue.code for issue in removal.value.issues] == ["LAST_OWNER"]
    assert [issue.code for issue in demotion.value.issues] == ["LAST_OWNER"]


async def test_an_owner_may_step_down_once_there_is_a_second_one(memberships, pool):
    """The rule is "at least one owner", not "owners are permanent".

    Without this, the check above would be satisfied by a service that simply
    refused every demotion, and handing a workspace over would be impossible.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    scope = await owner_scope(memberships)

    await memberships.update_member_role(
        scope=scope,
        user_id=COLLEAGUE_ID,
        role="owner",
    )

    stepped_down = await memberships.update_member_role(
        scope=scope,
        user_id=FOUNDER_ID,
        role="member",
    )

    assert stepped_down.role == "member"

    assert (
        await pool.fetchval(
            "SELECT count(*) FROM workspace_members WHERE role = 'owner'"
        )
        == 1
    )


# ------------------------------------------------------- removing a real member


async def seed_issue(pool, teams, memberships) -> tuple[UUID, UUID]:
    """A team and one issue in acme. Returns (workspace_id, issue_id).

    Every row a departing member can hold hangs off an issue, so this is the
    smallest fixture that lets the two tests below say anything.
    """
    scope = await owner_scope(memberships)
    team = await teams.create(scope=scope, name="Engineering", key="ENG")

    async with pool.acquire() as connection:
        issue_id = await connection.fetchval(
            """
            INSERT INTO issues (
                workspace_id, team_id, number, workflow_state_id, title, priority
            )
            VALUES (
                $1, $2, 1,
                (
                    SELECT id FROM workflow_states
                    WHERE workspace_id = $1 AND team_id = $2 AND type = 'unstarted'
                ),
                'Something to watch', 1
            )
            RETURNING id
            """,
            scope.workspace_id,
            team.team.id,
        )

    return scope.workspace_id, issue_id


async def test_a_member_holding_only_personal_rows_can_be_removed(
    memberships, teams, pool
):
    """Regression: one unread notification made a member unremovable.

    Seventeen foreign keys reference `workspace_members` and every one but the
    assignee is RESTRICT. That is right for the things a workspace shares --
    009 says a project lead must be reassigned rather than silently vacated --
    but it was never narrowed, so it also caught rows only the departing person
    could ever see, for a member whose entire footprint was a notification
    nobody had read.

    All four personal kinds are seeded at once, deliberately. Each is deleted by
    its own statement, so a fixture holding one of them would pass while the
    other three survived it.

    Since 026 the four deletions are no longer what makes the removal possible
    -- the membership row is stamped rather than deleted, so no RESTRICT is in
    the way of anything. They still have to happen, and the assertion is
    unchanged, because a former member whose subscriptions survived would go on
    being notified about issues they can no longer open.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    workspace_id, issue_id = await seed_issue(pool, teams, memberships)

    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO notifications (workspace_id, user_id, issue_id, kind) "
            "VALUES ($1, $2, $3, 'assigned')",
            workspace_id,
            COLLEAGUE_ID,
            issue_id,
        )
        await connection.execute(
            "INSERT INTO issue_subscribers (workspace_id, issue_id, user_id) "
            "VALUES ($1, $2, $3)",
            workspace_id,
            issue_id,
            COLLEAGUE_ID,
        )
        view_id = await connection.fetchval(
            """
            INSERT INTO saved_views (
                workspace_id, name, filter, order_field, order_direction,
                layout, visibility, created_by
            )
            VALUES ($1, 'Mine', '{}'::jsonb, 'created_at', 'desc',
                    'list', 'personal', $2)
            RETURNING id
            """,
            workspace_id,
            COLLEAGUE_ID,
        )
        # A favourite pointing at that very view, which is what makes the
        # deletion ORDER in delete_personal_rows load-bearing rather than
        # incidental: favorites_saved_view_fk is RESTRICT like everything else.
        await connection.execute(
            "INSERT INTO favorites (workspace_id, user_id, saved_view_id, position) "
            "VALUES ($1, $2, $3, 0)",
            workspace_id,
            COLLEAGUE_ID,
            view_id,
        )

    removed = await memberships.remove_member(
        scope=await owner_scope(memberships),
        user_id=COLLEAGUE_ID,
    )

    assert removed == COLLEAGUE_ID

    async with pool.acquire() as connection:
        for table, column in (
            ("notifications", "user_id"),
            ("issue_subscribers", "user_id"),
            ("favorites", "user_id"),
            ("saved_views", "created_by"),
        ):
            assert (
                await connection.fetchval(
                    f"SELECT count(*) FROM {table} WHERE {column} = $1",  # noqa: S608
                    COLLEAGUE_ID,
                )
                == 0
            ), f"{table} still holds a row for the removed member"

        # The membership itself is the one row that stays, stamped. Asserted
        # here rather than in a test of its own because this is the file's
        # first removal and the shape would otherwise be stated nowhere.
        assert (
            await connection.fetchval(
                "SELECT removed_at FROM workspace_members WHERE user_id = $1",
                COLLEAGUE_ID,
            )
            is not None
        )


async def test_a_member_who_leads_a_project_is_refused_by_name(
    memberships, teams, pool
):
    """The other half: shared things still refuse, but say which one.

    This is the policy 009 wrote down -- a lead is reassigned rather than
    silently vacated -- and the only thing that changed is that the refusal now
    arrives as a field error an admin can act on instead of as a 500. The
    membership must still be there afterwards: a removal that half-happened,
    clearing the personal rows and then failing, would be worse than the bug.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    scope = await owner_scope(memberships)

    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO projects (workspace_id, name, state, lead_id)
            VALUES ($1, 'Launch', 'planned', $2)
            """,
            scope.workspace_id,
            COLLEAGUE_ID,
        )

    with pytest.raises(ValidationError) as raised:
        await memberships.remove_member(scope=scope, user_id=COLLEAGUE_ID)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("userId", "STILL_LEADS_PROJECT")
    ]

    assert (
        await pool.fetchval(
            "SELECT count(*) FROM workspace_members WHERE user_id = $1 "
            "AND removed_at IS NULL",
            COLLEAGUE_ID,
        )
        == 1
    )


# ------------------------------------------------------------ member departure


async def seed_comment(pool, workspace_id: UUID, issue_id: UUID, author: UUID) -> UUID:
    """One comment by `author`, written straight into the table.

    Not through CommentService, unlike `join_as` above, and the difference is
    deliberate: this file is about what happens to the row afterwards, so the
    path that wrote it is not the subject and a service dependency here would
    be one more thing that can fail for reasons this test is not about.
    """
    async with pool.acquire() as connection:
        comment_id = await connection.fetchval(
            """
            INSERT INTO comments (workspace_id, issue_id, author_id, body)
            VALUES ($1, $2, $3, 'It is the cache, it is always the cache')
            RETURNING id
            """,
            workspace_id,
            issue_id,
            author,
        )

    return comment_id


async def test_a_member_who_commented_can_be_removed_and_keeps_the_comment(
    memberships, teams, pool
):
    """The bug 026 exists to fix, and the property it must not trade away.

    `comments_author_fk` is ON DELETE RESTRICT, so before 026 a member who had
    ever said anything could not be removed at all -- and unlike a project
    lead, there was no correction an admin could make, because a comment is not
    a mistake and cannot be handed to somebody else. Removal now stamps the
    membership instead of deleting it, so both halves hold at once: the person
    is gone, and the thing they wrote still says who wrote it.

    The join in the assertion is the part that matters. Checking that the
    comment row survived would prove much less -- what a reader needs is for
    `author_id` to still resolve to a membership, because that is what a
    comment thread renders a name from.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    workspace_id, issue_id = await seed_issue(pool, teams, memberships)
    comment_id = await seed_comment(pool, workspace_id, issue_id, COLLEAGUE_ID)

    removed = await memberships.remove_member(
        scope=await owner_scope(memberships),
        user_id=COLLEAGUE_ID,
    )

    assert removed == COLLEAGUE_ID

    attribution = await pool.fetchrow(
        """
        SELECT comments.body, comments.author_id, member.removed_at
        FROM comments
        JOIN workspace_members AS member
            ON member.workspace_id = comments.workspace_id
            AND member.user_id = comments.author_id
        WHERE comments.id = $1
        """,
        comment_id,
    )

    assert attribution is not None, "the comment no longer resolves to an author"
    assert attribution["author_id"] == COLLEAGUE_ID
    assert attribution["removed_at"] is not None


async def test_a_removed_member_cannot_authorize(memberships, teams, pool):
    """The half of the tombstone that would be a disaster to get wrong.

    Keeping the row is what makes authorship resolve, and it is also what would
    make a removed member keep every permission they had, since
    `AuthorizedWorkspaceScope` is built from exactly this table. The predicate
    in `find_membership` is the only thing standing between those two, so it is
    asserted through the service rather than by reading the SQL.

    Both refusals are checked, and the second is not a duplicate of the first:
    `membership_for_slug` answers "may I be here", and the workspace switcher
    answers "where may I be". A former member who kept the switcher entry would
    see a tenant that refuses them on arrival.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    # Real footprint before leaving, so the removal being tested is the one the
    # product performs rather than the trivial case of a member holding nothing.
    workspace_id, issue_id = await seed_issue(pool, teams, memberships)
    await seed_comment(pool, workspace_id, issue_id, COLLEAGUE_ID)

    assert await memberships.list_for_user(user_id=COLLEAGUE_ID) != []

    await memberships.remove_member(
        scope=await owner_scope(memberships),
        user_id=COLLEAGUE_ID,
    )

    with pytest.raises(WorkspaceAccessDeniedError):
        await memberships.authorized_scope_for_slug(slug="acme", user_id=COLLEAGUE_ID)

    assert await memberships.list_for_user(user_id=COLLEAGUE_ID) == []


async def test_a_removed_member_is_listed_as_former_rather_than_dropped(
    memberships, teams, pool
):
    """Absent from a picker, present in history, out of one query.

    `list_members` is read by two screens wanting opposite halves of it, so it
    returns both and marks the difference: an assignee picker filters on
    `removed_at`, and a comment thread looks the author up and finds them. The
    alternative -- filtering removed members out server-side -- would make
    every author who has left indistinguishable from a lookup that failed, and
    the two would render identically.

    The role is asserted too. A former member keeps the one they held, because
    that is what a history screen shows; it grants nothing, which is what the
    test above is about.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="admin")

    workspace_id, issue_id = await seed_issue(pool, teams, memberships)
    await seed_comment(pool, workspace_id, issue_id, COLLEAGUE_ID)

    await memberships.remove_member(
        scope=await owner_scope(memberships),
        user_id=COLLEAGUE_ID,
    )

    members = await memberships.list_members(scope=await owner_scope(memberships))
    by_id = {member.user_id: member for member in members}

    assert set(by_id) == {FOUNDER_ID, COLLEAGUE_ID}

    assert by_id[FOUNDER_ID].removed_at is None
    assert by_id[COLLEAGUE_ID].removed_at is not None
    assert by_id[COLLEAGUE_ID].role == "admin"

    assert [member.user_id for member in members if member.removed_at is None] == [
        FOUNDER_ID
    ], "the picker's half of this list still offers someone who left"


async def test_an_assigned_issue_is_unassigned_rather_than_blocking(
    memberships, teams, pool
):
    """What `issues_assignee_fk` used to do, now that it cannot.

    006 made that key `ON DELETE SET NULL (assignee_id)` alone among the
    seventeen: an assignment says who is doing the work now, so when the person
    leaves it is vacated rather than defended. Stamping the row fires no
    ON DELETE clause, so the behaviour moved into `unassign_issues` -- and if
    it ever stops running, the symptom is not an error but work sitting
    assigned to somebody who is not there, which nobody would be paged about.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    workspace_id, issue_id = await seed_issue(pool, teams, memberships)

    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE issues SET assignee_id = $1 WHERE id = $2",
            COLLEAGUE_ID,
            issue_id,
        )

    removed = await memberships.remove_member(
        scope=await owner_scope(memberships),
        user_id=COLLEAGUE_ID,
    )

    assert removed == COLLEAGUE_ID

    assert (
        await pool.fetchval("SELECT assignee_id FROM issues WHERE id = $1", issue_id)
        is None
    )


async def test_a_shared_view_refuses_the_removal_and_a_personal_one_does_not(
    memberships, teams, pool
):
    """`saved_views_creator_fk` is the probe with a branch, so it gets a test.

    Both rows are the same table, the same column and the same person; only
    `visibility` differs, and it decides everything. A personal view is deleted
    with the membership because nobody else could ever open it. A shared one is
    a list the workspace uses, so the removal is refused until somebody takes
    it over -- 009's answer for a project lead, applied to the thing 019 built.

    Asserting the refusal first and the removal second is what makes this one
    test rather than two: the shared view is the only difference between the
    two calls, so the second passing is what proves the first refusal was about
    that row and not about the personal one sitting beside it.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    scope = await owner_scope(memberships)

    async with pool.acquire() as connection:
        for visibility in ("personal", "shared"):
            await connection.execute(
                """
                INSERT INTO saved_views (
                    workspace_id, name, filter, order_field, order_direction,
                    layout, visibility, created_by
                )
                VALUES ($1, $2, '{}'::jsonb, 'created_at', 'desc',
                        'list', $3, $4)
                """,
                scope.workspace_id,
                f"{visibility} view",
                visibility,
                COLLEAGUE_ID,
            )

    with pytest.raises(ValidationError) as raised:
        await memberships.remove_member(scope=scope, user_id=COLLEAGUE_ID)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("userId", "OWNS_SHARED_VIEW")
    ]

    # Nothing was written by the refused attempt -- not even the personal view,
    # which the old removal path deleted before discovering the RESTRICT and
    # relied on a rollback to put back.
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM saved_views WHERE created_by = $1", COLLEAGUE_ID
        )
        == 2
    )

    async with pool.acquire() as connection:
        await connection.execute(
            "DELETE FROM saved_views WHERE created_by = $1 AND visibility = 'shared'",
            COLLEAGUE_ID,
        )

    assert (
        await memberships.remove_member(scope=scope, user_id=COLLEAGUE_ID)
        == COLLEAGUE_ID
    )


async def test_every_shared_holding_has_a_field_error_and_a_column(
    memberships, teams, pool
):
    """The two lists that have to agree, pinned instead of trusted.

    `shared_holdings` reads its row against `SHARED_HOLDINGS`, and
    `remove_member` looks every name it returns up in `_REMOVAL_BLOCKED` with a
    bare subscript. A name in the tuple and not the mapping is a KeyError on a
    real removal; a name in the mapping and not the tuple is a refusal that can
    never fire. Neither is visible by reading either file.

    Calling the repository is the other half. The statement is a complete SQL
    literal and is deliberately NOT built from the tuple -- no SQL in this
    codebase is assembled from Python -- so a name the tuple has and the select
    list does not would raise here and nowhere else.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    assert set(SHARED_HOLDINGS) == set(_REMOVAL_BLOCKED)

    scope = await owner_scope(memberships)

    async with pool.acquire() as connection:
        assert (
            await MembershipRepository().shared_holdings(
                connection,
                workspace_id=scope.workspace_id,
                user_id=COLLEAGUE_ID,
            )
            == []
        )


async def test_a_removed_member_can_be_invited_back(memberships, teams, pool):
    """A tombstone occupies the primary key, so rejoining has to revive it.

    Without the ON CONFLICT in `create`, the second grant collides with the
    stamped row, the service reports "you are already a member" of a workspace
    the person cannot open, and the rollback puts the invitation back -- so the
    same token can be presented forever and always says the same thing. There
    is no way out of that state through the API at all.

    `created_at` moving is asserted because `ON CONFLICT DO UPDATE` touches
    only the columns it names: left alone, the date the product shows as
    "joined" would be the original one and would span the stretch when this
    person was not in the workspace.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="member")

    workspace_id, issue_id = await seed_issue(pool, teams, memberships)
    await seed_comment(pool, workspace_id, issue_id, COLLEAGUE_ID)

    first_joined = await pool.fetchval(
        "SELECT created_at FROM workspace_members WHERE user_id = $1", COLLEAGUE_ID
    )

    await memberships.remove_member(
        scope=await owner_scope(memberships),
        user_id=COLLEAGUE_ID,
    )

    rejoined = await join_as(memberships, user_id=COLLEAGUE_ID, role="admin")

    assert rejoined.workspace_slug == "acme"
    assert rejoined.role == "admin"
    assert rejoined.created_at > first_joined

    # They can authorize again, and the invitation granted the role it named
    # rather than the one they used to hold.
    scope = await memberships.authorized_scope_for_slug(
        slug="acme", user_id=COLLEAGUE_ID
    )

    assert scope.role == "admin"

    assert (
        await pool.fetchval(
            "SELECT removed_at FROM workspace_members WHERE user_id = $1",
            COLLEAGUE_ID,
        )
        is None
    )


async def test_a_second_grant_to_a_current_member_still_refuses(memberships):
    """The `WHERE` on the conflict action, which is the dangerous half.

    `ON CONFLICT DO UPDATE` without it would rewrite a current member's role
    from whatever the invitation happened to say -- a privilege change
    available to anybody holding any invitation to a workspace the account is
    already in, including one they were sent themselves at a lower role. The
    predicate confines the revival to rows that were actually removed, and this
    is the case that proves it: an active member's role is untouched and the
    acceptance is refused.
    """
    await acme(memberships)
    await join_as(memberships, user_id=COLLEAGUE_ID, role="admin")

    scope = await owner_scope(memberships)

    _, token = await memberships.create_invitation(
        scope=scope,
        email="colleague@example.test",
        role="member",
    )

    with pytest.raises(ValidationError) as raised:
        await memberships.accept_invitation(token=token, user_id=COLLEAGUE_ID)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("token", "ALREADY_MEMBER")
    ]

    unchanged = await memberships.authorized_scope_for_slug(
        slug="acme", user_id=COLLEAGUE_ID
    )

    assert unchanged.role == "admin"
