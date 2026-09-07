"""Migration 019 applied by the real runner, then asked what it built.

019 stores two things that are unusually easy to get wrong, and this file is
about the second of each pair.

The first is a saved view: a name, some display choices, and a FILTER that the
issue list is later executed with. The filter is a JSONB document, which means
its bytes were chosen by whoever created the view -- so the interesting
question is not "does it round-trip" (tests/test_saved_views.py answers that
without a database) but "does the list it produces stay inside the tenant it
was saved in". The round-trip tests at the top of this file answer it against
a real PostgreSQL, through the real repositories, because the claim spans the
codec, the column and the predicate builder and no one of them can be trusted
about it alone.

The second is a favourite: a per-user, per-workspace pointer at a team, a
project or a saved view. Three nullable target columns, and the reason they
are not one `(kind, target_id)` pair is that a polymorphic pair can carry no
foreign key -- so "favourite another workspace's project" would be a row
PostgreSQL happily stores. Most of the assertions below are therefore about
rows PostgreSQL will not store:

  * a saved view labelled with another workspace's team;
  * a saved view authored by someone who is not a member of its workspace;
  * a favourite pointing at another workspace's project or saved view;
  * a favourite owned by a non-member;
  * a favourite claiming to BE the other workspace;
  * a favourite pointing at nothing, or at two things at once;
  * a subgrouping with no grouping under it;
  * a filter that is not a JSON object.

And two that are about the shape being usable rather than safe: three
favourites of three different kinds coexist for one person (the NULLs in those
unique constraints have to stay DISTINCT for that), and deleting a view or a
member that something still points at is refused rather than silently
cascading.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

import json
from uuid import UUID

import asyncpg
import pytest

from app.domain.issues import DEFAULT_ORDER, IssueFilter, IssueOrder, IssueOrderField
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.repositories.saved_views import FavoriteRepository, SavedViewRepository

from tests.conftest import (
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

# The tenant 002 seeds, named there as literals precisely so a test can assert
# against a constant instead of querying for the value it is about to check.
BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")
BOOTSTRAP_TEAM_ID = UUID("00000000-0000-7000-8000-000000000002")

# The other tenant: a real workspace, with a real team, a real member, a real
# project and a real saved view, whose rows every cross-tenant assertion below
# tries and fails to reach. It has to be real -- a nonexistent id would be
# refused by any spelling of these constraints and would prove nothing about
# which one is in force.
OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

# A second member of the bootstrap workspace, so "somebody else's personal
# view" is a case inside one tenant rather than only across two.
COLLEAGUE_ID = UUID("00000000-0000-7000-8000-0000000000e3")

BOOTSTRAP_PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c1")
OTHER_PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000c2")

SCOPE = WorkspaceScope(workspace_id=BOOTSTRAP_WORKSPACE_ID)
OTHER_SCOPE = WorkspaceScope(workspace_id=OTHER_WORKSPACE_ID)

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
    $1, $2, $3, $4,
    (
        SELECT id FROM workflow_states
        WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
    ),
    $5, 1, $6
)
"""

INSERT_VIEW_SQL = """
INSERT INTO saved_views (
    id, workspace_id, created_by, team_id, name, filter,
    order_field, order_direction, layout, grouping, subgrouping, visibility
)
VALUES ($1, $2, $3, $4, $5, $6::JSONB, $7, $8, $9, $10, $11, $12)
"""

INSERT_FAVORITE_SQL = """
INSERT INTO favorites
    (workspace_id, user_id, team_id, project_id, saved_view_id, position)
VALUES ($1, $2, $3, $4, $5, $6)
"""


@pytest.fixture
async def connection(postgres_dsn):
    """A migrated database with two fully-populated tenants."""
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
    """Two workspaces, each with a team, a member, a project and a saved view.

    Symmetric on purpose. Every cross-tenant assertion below is "workspace A's
    row pointing at workspace B's row", and a lopsided fixture -- where one
    side lacks the thing the other is reaching for -- would pass those
    assertions for the wrong reason.
    """
    await connection.execute(
        "INSERT INTO workspaces (id, slug, name) VALUES ($1, 'other', 'Other')",
        OTHER_WORKSPACE_ID,
    )
    await connection.execute(
        "INSERT INTO teams (id, workspace_id, name, key) "
        "VALUES ($1, $2, 'Other', 'OTH')",
        OTHER_TEAM_ID,
        OTHER_WORKSPACE_ID,
    )

    # 005 seeds workflow states for the teams that exist when it runs, so the
    # team created above -- afterwards -- has none, and an issue on it cannot
    # be inserted at all.
    await seed_workflow_states(connection, OTHER_WORKSPACE_ID, OTHER_TEAM_ID)

    for user_id in (MEMBER_ID, OUTSIDER_ID, COLLEAGUE_ID):
        await connection.execute(INSERT_USER_SQL, user_id)

    for workspace_id, user_id in (
        (BOOTSTRAP_WORKSPACE_ID, MEMBER_ID),
        (BOOTSTRAP_WORKSPACE_ID, COLLEAGUE_ID),
        (OTHER_WORKSPACE_ID, OUTSIDER_ID),
    ):
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES ($1, $2, 'admin')",
            workspace_id,
            user_id,
        )

    for project_id, workspace_id, name in (
        (BOOTSTRAP_PROJECT_ID, BOOTSTRAP_WORKSPACE_ID, "Ours"),
        (OTHER_PROJECT_ID, OTHER_WORKSPACE_ID, "Theirs"),
    ):
        await connection.execute(
            "INSERT INTO projects (id, workspace_id, name, state) "
            "VALUES ($1, $2, $3, 'started')",
            project_id,
            workspace_id,
            name,
        )


async def insert_view(
    connection,
    view_id: UUID,
    *,
    workspace_id: UUID = BOOTSTRAP_WORKSPACE_ID,
    created_by: UUID = MEMBER_ID,
    team_id: UUID | None = None,
    name: str = "My work",
    filter_json: str = "{}",
    order_field: str = "created_at",
    order_direction: str = "desc",
    layout: str = "list",
    grouping: str | None = None,
    subgrouping: str | None = None,
    visibility: str = "personal",
) -> None:
    await connection.execute(
        INSERT_VIEW_SQL,
        view_id,
        workspace_id,
        created_by,
        team_id,
        name,
        filter_json,
        order_field,
        order_direction,
        layout,
        grouping,
        subgrouping,
        visibility,
    )


# --- the round trip, against a real database --------------------------


async def test_a_saved_filter_selects_the_same_issues_it_was_saved_from(connection):
    """The claim the whole feature rests on, end to end.

    Three issues, two of them assigned. A filter is stored through
    SavedViewRepository, read back, and handed straight to IssueRepository --
    the same object `issues(filter:)` hands it -- and the page that comes back
    has to be the page the filter named. Nothing here reconstructs the filter
    by hand: if the codec, the JSONB column or the predicate builder disagreed
    about any field, this is where it shows.
    """
    repository = SavedViewRepository()
    issues = IssueRepository()

    for index, assignee in enumerate((MEMBER_ID, MEMBER_ID, None), start=1):
        await connection.execute(
            INSERT_ISSUE_SQL,
            UUID(int=index),
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
            index,
            f"Issue {index}",
            assignee,
        )

    saved = await repository.create(
        connection,
        scope=SCOPE,
        creator_id=MEMBER_ID,
        team_id=None,
        name="Assigned to me",
        issue_filter=IssueFilter(assignee_id=MEMBER_ID),
        order=DEFAULT_ORDER,
        layout="list",
        grouping=None,
        subgrouping=None,
        visibility="personal",
    )

    loaded = await repository.get_by_id(
        connection,
        scope=SCOPE,
        viewer_id=MEMBER_ID,
        saved_view_id=saved.id,
    )

    rows = await issues.list(
        connection,
        scope=SCOPE,
        issue_filter=loaded.issue_filter,
        order=loaded.order,
        limit=10,
        after=None,
    )

    assert [issue.number for issue in rows] == [2, 1]


async def test_a_saved_filter_for_the_unassigned_issues_stays_that_filter(connection):
    """The tri-state, through JSONB and back.

    `assignee_id=None` means `assignee_id IS NULL` -- the unassigned issues --
    and is stored as a JSON null under a key that is present. A codec that
    wrote it as an absent key, or a reader that treated null as "no filter",
    would turn this saved view into "every issue in the workspace". Both
    failures are silent and both are wider than what was saved, which is why
    this is asserted separately from the case above.
    """
    repository = SavedViewRepository()
    issues = IssueRepository()

    for index, assignee in enumerate((MEMBER_ID, None), start=1):
        await connection.execute(
            INSERT_ISSUE_SQL,
            UUID(int=index),
            BOOTSTRAP_WORKSPACE_ID,
            BOOTSTRAP_TEAM_ID,
            index,
            f"Issue {index}",
            assignee,
        )

    saved = await repository.create(
        connection,
        scope=SCOPE,
        creator_id=MEMBER_ID,
        team_id=None,
        name="Unassigned",
        issue_filter=IssueFilter(assignee_id=None),
        order=IssueOrder(field=IssueOrderField.CREATED_AT),
        layout="list",
        grouping=None,
        subgrouping=None,
        visibility="shared",
    )

    stored = await connection.fetchval(
        "SELECT filter FROM saved_views WHERE id = $1", saved.id
    )

    assert json.loads(stored) == {"assignee_id": None}

    loaded = await repository.get_by_id(
        connection,
        scope=SCOPE,
        viewer_id=COLLEAGUE_ID,
        saved_view_id=saved.id,
    )

    rows = await issues.list(
        connection,
        scope=SCOPE,
        issue_filter=loaded.issue_filter,
        order=loaded.order,
        limit=10,
        after=None,
    )

    assert [issue.number for issue in rows] == [2]


# --- the list statement -----------------------------------------------
#
# One statement carrying four independent concerns -- the tenant, the viewer's
# visibility, an optional null-tolerant team narrowing and a keyset resume --
# which is more than any other query this migration serves. Each of the four is
# a way to hand back rows nobody asked for, so each gets its own assertion.


async def test_a_list_holds_shared_views_and_the_viewers_own(connection):
    """The visibility predicate, in the place it decides most.

    `get_by_id` refusing a colleague's personal view protects one row at a
    time; the list is where a missing predicate would hand over every private
    view in the workspace at once. Three views, three cases: shared (visible),
    mine (visible), theirs (not).
    """
    repository = SavedViewRepository()

    await insert_view(connection, UUID(int=1), name="Shared", visibility="shared")
    await insert_view(connection, UUID(int=2), name="Mine", visibility="personal")
    await insert_view(
        connection,
        UUID(int=3),
        name="Theirs",
        created_by=COLLEAGUE_ID,
        visibility="personal",
    )

    rows = await repository.list(
        connection,
        scope=SCOPE,
        viewer_id=MEMBER_ID,
        filter_by_team=False,
        team_id=None,
        limit=10,
        after_name=None,
        after_id=None,
    )

    assert [view.name for view in rows] == ["Mine", "Shared"]


async def test_narrowing_to_no_team_is_not_the_same_as_not_narrowing(connection):
    """The two arguments the team filter needs, and why one will not do.

    "Views filed under no team" and "views under any team" are different
    requests, and a single nullable `team_id` cannot tell the second from the
    first -- `= NULL` is never true, so a naive equality would answer the
    workspace-wide request with nothing at all.
    """
    repository = SavedViewRepository()

    await insert_view(connection, UUID(int=1), name="Everywhere", team_id=None)
    await insert_view(
        connection, UUID(int=2), name="Engineering", team_id=BOOTSTRAP_TEAM_ID
    )

    async def names(*, filter_by_team, team_id):
        rows = await repository.list(
            connection,
            scope=SCOPE,
            viewer_id=MEMBER_ID,
            filter_by_team=filter_by_team,
            team_id=team_id,
            limit=10,
            after_name=None,
            after_id=None,
        )

        return [view.name for view in rows]

    assert await names(filter_by_team=False, team_id=None) == [
        "Engineering",
        "Everywhere",
    ]
    assert await names(filter_by_team=True, team_id=None) == ["Everywhere"]
    assert await names(filter_by_team=True, team_id=BOOTSTRAP_TEAM_ID) == [
        "Engineering"
    ]


async def test_the_keyset_walk_neither_skips_nor_repeats_a_view(connection):
    """Pages of one over five views, two of which share a name.

    The duplicate name is the point: `(name, id) > ($5, $6)` is a row-value
    comparison and `id` is what makes the order total. Comparing on `name`
    alone would either serve one of the twins twice or lose the other, and
    neither failure raises anything.
    """
    repository = SavedViewRepository()

    for index, name in enumerate(("Alpha", "Beta", "Beta", "Delta", "Gamma"), start=1):
        await insert_view(connection, UUID(int=index), name=name)

    seen = []
    cursor_name = None
    cursor_id = None

    for _ in range(5):
        rows = await repository.list(
            connection,
            scope=SCOPE,
            viewer_id=MEMBER_ID,
            filter_by_team=False,
            team_id=None,
            limit=1,
            after_name=cursor_name,
            after_id=cursor_id,
        )

        assert len(rows) == 1

        seen.append((rows[0].name, rows[0].id))
        cursor_name, cursor_id = rows[0].name, rows[0].id

    assert [name for name, _ in seen] == ["Alpha", "Beta", "Beta", "Delta", "Gamma"]
    assert len({view_id for _, view_id in seen}) == 5

    # And the walk ends rather than restarting.
    assert (
        await repository.list(
            connection,
            scope=SCOPE,
            viewer_id=MEMBER_ID,
            filter_by_team=False,
            team_id=None,
            limit=1,
            after_name=cursor_name,
            after_id=cursor_id,
        )
        == []
    )


async def test_a_list_never_leaves_its_workspace(connection):
    """The tenant predicate is ANDed with the cursor rather than folded into
    it. A row-value comparison widened to include `workspace_id` would put
    workspaces into the ordering, which is how a page walk falls out of one
    tenant and into whichever one sorts next."""
    repository = SavedViewRepository()

    await insert_view(connection, UUID(int=1), name="Aardvark", visibility="shared")
    await insert_view(
        connection,
        UUID(int=2),
        name="Aardvark",
        workspace_id=OTHER_WORKSPACE_ID,
        created_by=OUTSIDER_ID,
        visibility="shared",
    )

    rows = await repository.list(
        connection,
        scope=SCOPE,
        viewer_id=MEMBER_ID,
        filter_by_team=False,
        team_id=None,
        limit=10,
        after_name=None,
        after_id=None,
    )

    assert [view.id for view in rows] == [UUID(int=1)]


async def test_a_saved_view_cannot_be_read_out_of_another_workspace(connection):
    """A view's id is not a capability.

    The workspace is part of the lookup rather than a check applied
    afterwards, so a caller holding a leaked id and their own tenant gets the
    same nothing an id that exists nowhere gets.
    """
    repository = SavedViewRepository()

    saved = await repository.create(
        connection,
        scope=OTHER_SCOPE,
        creator_id=OUTSIDER_ID,
        team_id=None,
        name="Theirs",
        issue_filter=IssueFilter(),
        order=DEFAULT_ORDER,
        layout="list",
        grouping=None,
        subgrouping=None,
        visibility="shared",
    )

    assert (
        await repository.get_by_id(
            connection,
            scope=SCOPE,
            viewer_id=MEMBER_ID,
            saved_view_id=saved.id,
        )
        is None
    )


async def test_a_personal_view_is_invisible_to_another_member(connection):
    """Shared is a grant of sight; personal is not, inside one tenant.

    The predicate is in the statement rather than applied to the result, so
    this is the same nothing a view in another workspace produces -- a
    colleague cannot tell "you may not see it" from "there is no such view",
    and so cannot enumerate other people's private views by id.
    """
    repository = SavedViewRepository()

    saved = await repository.create(
        connection,
        scope=SCOPE,
        creator_id=MEMBER_ID,
        team_id=None,
        name="Mine alone",
        issue_filter=IssueFilter(),
        order=DEFAULT_ORDER,
        layout="list",
        grouping=None,
        subgrouping=None,
        visibility="personal",
    )

    assert (
        await repository.get_by_id(
            connection,
            scope=SCOPE,
            viewer_id=COLLEAGUE_ID,
            saved_view_id=saved.id,
        )
        is None
    )

    assert (
        await repository.get_by_id(
            connection,
            scope=SCOPE,
            viewer_id=MEMBER_ID,
            saved_view_id=saved.id,
        )
        is not None
    )


# --- the cross-tenant refusals ----------------------------------------


async def test_a_saved_view_cannot_be_labelled_with_another_workspaces_team(connection):
    """One workspace_id for the row, so this pairing has nowhere to be spelled.

    The row claims to be in the bootstrap workspace and points at the other
    workspace's team. saved_views_team_fk reads the same workspace_id for both
    halves, so the pair simply does not exist -- and the other workspace's team
    cannot appear in this workspace's sidebar.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await insert_view(connection, UUID(int=1), team_id=OTHER_TEAM_ID)

    assert raised.value.constraint_name == "saved_views_team_fk"


async def test_a_saved_view_cannot_be_authored_by_a_non_member(connection):
    """`created_by` is not decoration -- it is the read predicate.

    A view whose author belongs to another workspace would be one that no
    member of THIS workspace can see while it is personal, and that none of
    them can edit or delete at all. The composite key onto workspace_members
    refuses it in the same statement that would have written the row, so there
    is no window for a membership to be revoked in.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await insert_view(connection, UUID(int=1), created_by=OUTSIDER_ID)

    assert raised.value.constraint_name == "saved_views_creator_fk"


async def test_a_favorite_cannot_point_at_another_workspaces_project(connection):
    """The attack the three typed columns exist for.

    A `(kind, target_id)` pair would carry no foreign key at all, so this row
    would be stored and only a resolver remembering to scope its lookup would
    stand between it and another tenant's project name in a sidebar. There is
    no column for the second workspace to go in.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_FAVORITE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
            None,
            OTHER_PROJECT_ID,
            None,
            0,
        )

    assert raised.value.constraint_name == "favorites_project_fk"


async def test_a_favorite_cannot_point_at_another_workspaces_saved_view(connection):
    """The same attack through the third target, refused the same way.

    Asserted separately rather than assumed from the project case: these are
    two constraints on two columns, and the saved-view one is the easier of
    the pair to declare wrong because `saved_views` is a table this same
    migration creates -- so its unique key could have been left at `id` alone
    without anything else in the file noticing.
    """
    await insert_view(
        connection,
        UUID(int=9),
        workspace_id=OTHER_WORKSPACE_ID,
        created_by=OUTSIDER_ID,
        visibility="shared",
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_FAVORITE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
            None,
            None,
            UUID(int=9),
            0,
        )

    assert raised.value.constraint_name == "favorites_saved_view_fk"


async def test_a_favorite_cannot_be_owned_by_a_non_member(connection):
    """Per-user AND per-workspace, in one constraint.

    A key onto `users` would accept a favourite owned by someone with no
    membership here at all -- an ex-member's sidebar, still holding pointers
    into a workspace they were removed from.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_FAVORITE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            OUTSIDER_ID,
            BOOTSTRAP_TEAM_ID,
            None,
            None,
            0,
        )

    assert raised.value.constraint_name == "favorites_member_fk"


async def test_a_favorite_cannot_smuggle_a_second_workspace_through_its_own_id(
    connection,
):
    """The other direction: claim to BE the other workspace.

    A row whose workspace_id is the other tenant's, whose user is a member
    there, pointing at THIS tenant's project. It fails on the project key,
    which is the point -- both halves are checked against the one column, so
    whichever way round an attacker spells the mismatch, one of the
    constraints is looking at it.
    """
    with pytest.raises(asyncpg.ForeignKeyViolationError) as raised:
        await connection.execute(
            INSERT_FAVORITE_SQL,
            OTHER_WORKSPACE_ID,
            OUTSIDER_ID,
            None,
            BOOTSTRAP_PROJECT_ID,
            None,
            0,
        )

    assert raised.value.constraint_name == "favorites_project_fk"


async def test_favoriting_another_members_personal_view_selects_no_row(connection):
    """The one refusal the schema cannot make, made by the statement instead.

    Visibility is a comparison between two tables' columns, which is a JOIN
    and not a constraint -- `favorites_saved_view_fk` checks the tenant and
    stops there. So the guard is a `WHERE EXISTS` inside the same INSERT, and
    a view this person may not read simply writes no row. Reported to the
    caller as the same NOT_FOUND a nonexistent id gets.
    """
    favorites = FavoriteRepository()

    await insert_view(connection, UUID(int=7), created_by=COLLEAGUE_ID)

    assert (
        await favorites.add(
            connection,
            scope=SCOPE,
            user_id=MEMBER_ID,
            team_id=None,
            project_id=None,
            saved_view_id=UUID(int=7),
        )
        is None
    )

    # The colleague's own view is theirs to favourite, so the guard is not
    # merely refusing everything.
    assert (
        await favorites.add(
            connection,
            scope=SCOPE,
            user_id=COLLEAGUE_ID,
            team_id=None,
            project_id=None,
            saved_view_id=UUID(int=7),
        )
        is not None
    )


# --- the shape of a row -----------------------------------------------


@pytest.mark.parametrize(
    "targets",
    [
        (None, None, None),
        (BOOTSTRAP_TEAM_ID, BOOTSTRAP_PROJECT_ID, None),
    ],
    ids=["nothing", "two things"],
)
async def test_a_favorite_points_at_exactly_one_thing(connection, targets):
    """Not zero -- a position in a list with nothing at it -- and not two,
    which would be one row that has to render twice and be un-favourited
    twice."""
    team_id, project_id, saved_view_id = targets

    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await connection.execute(
            INSERT_FAVORITE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
            team_id,
            project_id,
            saved_view_id,
            0,
        )

    assert raised.value.constraint_name == "favorites_one_target"


async def test_three_kinds_of_favorite_coexist_for_one_person(connection):
    """The property the three separate UNIQUE constraints rest on.

    NULLs are DISTINCT in a unique constraint by default, so every row
    favouriting a project holds `team_id IS NULL` and those NULLs do not
    collide. A single UNIQUE over all three columns would have constrained
    nothing at all, and one written NULLS NOT DISTINCT would refuse this --
    a sidebar that can hold one team and nothing else.
    """
    await insert_view(connection, UUID(int=3))

    for position, target in enumerate(
        (
            (BOOTSTRAP_TEAM_ID, None, None),
            (None, BOOTSTRAP_PROJECT_ID, None),
            (None, None, UUID(int=3)),
        )
    ):
        await connection.execute(
            INSERT_FAVORITE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
            *target,
            position,
        )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM favorites WHERE user_id = $1", MEMBER_ID
        )
        == 3
    )


async def test_the_same_thing_cannot_be_favorited_twice(connection):
    """One row per person per thing, so a double-click is a refusal rather
    than two entries in a sidebar that both have to be removed."""
    await connection.execute(
        INSERT_FAVORITE_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        MEMBER_ID,
        BOOTSTRAP_TEAM_ID,
        None,
        None,
        0,
    )

    with pytest.raises(asyncpg.UniqueViolationError) as raised:
        await connection.execute(
            INSERT_FAVORITE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
            BOOTSTRAP_TEAM_ID,
            None,
            None,
            1,
        )

    assert raised.value.constraint_name == "favorites_team_key"


async def test_two_people_favorite_the_same_thing_independently(connection):
    """`user_id` is inside each unique key, so a favourite is one person's.

    Without it the first member to favourite a team would take it off
    everybody else's sidebar, which is the failure a workspace-wide
    uniqueness would produce and which no error message would explain.
    """
    for user_id in (MEMBER_ID, COLLEAGUE_ID):
        await connection.execute(
            INSERT_FAVORITE_SQL,
            BOOTSTRAP_WORKSPACE_ID,
            user_id,
            BOOTSTRAP_TEAM_ID,
            None,
            None,
            0,
        )

    assert (
        await connection.fetchval(
            "SELECT count(*) FROM favorites WHERE team_id = $1", BOOTSTRAP_TEAM_ID
        )
        == 2
    )


async def test_a_filter_that_is_not_an_object_is_refused(connection):
    """The schema's half of the decoder's contract.

    `decode_filter` refuses an array, a number and a bare null too -- it has
    to, since it runs against rows that arrived any way at all -- but a
    document like this reaching the column would be a saved view that fails
    loudly in front of a user every time they open it. Caught before the row
    exists.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_view(connection, UUID(int=1), filter_json="[]")

    assert raised.value.constraint_name == "saved_views_filter_is_object"


async def test_a_subgrouping_needs_a_grouping_under_it(connection):
    """ "No grouping, subgrouped by assignee" is not a rendering any client
    can produce, so it would be stored, returned, and silently dropped on the
    way to the screen."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_view(connection, UUID(int=1), subgrouping="assignee")

    assert raised.value.constraint_name == "saved_views_subgrouping_requires_grouping"


async def test_a_subgrouping_must_differ_from_its_grouping(connection):
    """Grouped by assignee and subgrouped by assignee is one group per
    group -- a second level that adds no level."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_view(
            connection,
            UUID(int=1),
            grouping="assignee",
            subgrouping="assignee",
        )

    assert raised.value.constraint_name == "saved_views_subgrouping_requires_grouping"


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("layout", "kanbanish", "saved_views_layout_check"),
        ("grouping", "label", "saved_views_grouping_check"),
        ("visibility", "team", "saved_views_visibility_check"),
        ("order_field", "title", "saved_views_order_field_check"),
        ("order_direction", "ascending", "saved_views_order_direction_check"),
    ],
)
async def test_an_invented_vocabulary_value_is_refused(
    connection, column, value, constraint
):
    """Five closed vocabularies, five CHECKs.

    Each value here is a plausible typo rather than nonsense -- 'label' is a
    grouping Linear has and this schema deliberately does not, 'team' is a
    visibility that would need a team membership model to mean anything, and
    'ascending' is what someone writes for 'asc'. A typo that reaches the
    table is a view that renders as nothing, reported nowhere.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_view(connection, UUID(int=1), **{column: value})

    assert raised.value.constraint_name == constraint


async def test_an_empty_name_is_refused(connection):
    """A view with no name is one nobody can pick out of a sidebar, and an
    unbounded one is a way to make an index enormous by sending a large
    string."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await insert_view(connection, UUID(int=1), name="")

    assert raised.value.constraint_name == "saved_views_name_length"


# --- what a delete must not silently do -------------------------------


async def test_deleting_a_view_does_not_silently_empty_somebody_elses_sidebar(
    connection,
):
    """RESTRICT, not CASCADE.

    A favourite of a shared view belongs to whoever favourited it, so
    `DELETE FROM saved_views WHERE id = ...` under CASCADE would remove rows
    from other members' sidebars while reporting `DELETE 1`. SavedViewService
    clears them itself, in order, in the same transaction -- so the constraint
    is a guard on that ordering rather than an obstacle to it.

    `RestrictViolationError`, not `ForeignKeyViolationError`. The two are
    siblings under IntegrityConstraintViolationError rather than one being the
    other's parent, and PostgreSQL raises them for opposite situations:
    catching the wrong one here would pass for a schema that had no constraint
    at all, since the delete would then simply succeed and raise nothing.
    """
    await insert_view(connection, UUID(int=5), visibility="shared")
    await connection.execute(
        INSERT_FAVORITE_SQL,
        BOOTSTRAP_WORKSPACE_ID,
        COLLEAGUE_ID,
        None,
        None,
        UUID(int=5),
        0,
    )

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute("DELETE FROM saved_views WHERE id = $1", UUID(int=5))


async def test_removing_a_member_does_not_silently_discard_their_views(connection):
    """The same rule one table up, on the path of every member removal.

    Nulling `created_by` is not an option -- it is half of the read predicate,
    so a view with no author is one nobody can ever see or delete again -- and
    deleting the views silently is a `DELETE 1` that destroyed somebody's
    saved work. The caller decides.
    """
    await insert_view(connection, UUID(int=6))

    with pytest.raises(asyncpg.RestrictViolationError):
        await connection.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            BOOTSTRAP_WORKSPACE_ID,
            MEMBER_ID,
        )
