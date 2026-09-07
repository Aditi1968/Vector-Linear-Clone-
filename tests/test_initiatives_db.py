"""InitiativeService and the dependency half of ProjectService, against a real
PostgreSQL 18.

tests/test_migration_022_db.py proves what the schema refuses. This file proves
what only the SERVICES can refuse, and it exists because those two sets do not
overlap: a CHECK constraint sees one row, so `initiatives_parent_not_self` and
`project_dependencies_not_self` stop A -> A and nothing longer. Every guard
below reads rows to decide, which means it cannot be a constraint, which means
it can only be tested against a running server.

Four things are pinned here:

  * the initiative hierarchy refuses a cycle at ANY depth, not just at one --
    the two-step case and the three-step case are separate tests, because the
    recursive walk is exactly the kind of code that is right for the depth its
    author happened to try;
  * nesting is bounded, and bounded over BOTH ends of a move: a sub-tree that
    is itself within the limit is still refused when the parent it is moving
    under is deep enough that the combination is not;
  * the project dependency graph refuses a cycle at any depth, and a
    self-dependency before it takes a connection at all;
  * every cross-workspace refusal the composite keys make arrives as a field
    error a client can act on, rather than as a masked internal error -- which
    is the half `_CONSTRAINT_ERRORS` can be wrong about in a way no unit test
    reaches.

Marked `db`: deselected by default, skipped when Docker is unreachable.
"""

from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import ValidationError
from app.domain.initiatives import MAX_INITIATIVE_DEPTH
from app.domain.tenancy import WorkspaceScope
from app.repositories.initiatives import InitiativeRepository
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository
from app.services.initiatives import InitiativeService
from app.services.projects import ProjectService

from tests.conftest import apply_all_migrations, reset_schema


pytestmark = pytest.mark.db

BOOTSTRAP_WORKSPACE_ID = UUID("00000000-0000-7000-8000-000000000001")

OTHER_WORKSPACE_ID = UUID("00000000-0000-7000-8000-0000000000a1")
OTHER_TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a2")

MEMBER_ID = UUID("00000000-0000-7000-8000-0000000000e1")
OUTSIDER_ID = UUID("00000000-0000-7000-8000-0000000000e2")

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


@pytest.fixture
async def pool(postgres_dsn):
    """Two tenants, two accounts -- one member of each workspace.

    Symmetric on purpose: every cross-tenant assertion here is "workspace A's
    row reaching for workspace B's", and a fixture where the far side did not
    really exist would pass those assertions for the wrong reason.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)

        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
            OTHER_WORKSPACE_ID,
            "acme",
            "Acme",
        )
        await connection.execute(
            "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
            OTHER_TEAM_ID,
            OTHER_WORKSPACE_ID,
            "Acme Core",
            "ACME",
        )

        for user_id, workspace_id in (
            (MEMBER_ID, BOOTSTRAP_WORKSPACE_ID),
            (OUTSIDER_ID, OTHER_WORKSPACE_ID),
        ):
            await connection.execute(INSERT_USER_SQL, user_id)
            await connection.execute(
                "INSERT INTO workspace_members (workspace_id, user_id, role) "
                "VALUES ($1, $2, $3)",
                workspace_id,
                user_id,
                "member",
            )
    finally:
        await connection.close()

    created = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

    try:
        yield created
    finally:
        await created.close()


@pytest.fixture
def initiatives(pool) -> InitiativeService:
    """The real service over the real repository over the real database."""
    return InitiativeService(pool=pool, repository=InitiativeRepository())


@pytest.fixture
def projects(pool) -> ProjectService:
    return ProjectService(
        pool=pool,
        repository=ProjectRepository(),
        issue_repository=IssueRepository(),
        initiative_repository=InitiativeRepository(),
    )


async def make_initiative(service, name, *, scope=SCOPE):
    return await service.create(
        scope=scope,
        name=name,
        description=None,
        status="planned",
        target_date=None,
    )


async def make_project(service, name, *, scope=SCOPE):
    return await service.create(
        scope=scope,
        name=name,
        description=None,
        state="planned",
        target_date=None,
    )


async def make_chain(service, length: int):
    """`length` initiatives, each nested under the one before it.

    Returned root-first, so `chain[0]` is the top-level one and `chain[-1]` is
    the deepest. Built through `set_parent` rather than by writing the column,
    so the fixture itself goes through the guard under test -- a chain assembled
    behind the service's back could be deeper than the service would ever allow
    and would make the depth assertions meaningless.
    """
    chain = [await make_initiative(service, "Level 0")]

    for level in range(1, length):
        child = await make_initiative(service, f"Level {level}")
        await service.set_parent(
            scope=SCOPE,
            initiative_id=child.id,
            parent_id=chain[-1].id,
        )
        chain.append(child)

    return chain


def codes(exc: ValidationError) -> list[str]:
    return [issue.code for issue in exc.value.issues]


def fields(exc: ValidationError) -> list[str]:
    return [issue.field for issue in exc.value.issues]


# ------------------------------------------------- the hierarchy: cycles


async def test_an_initiative_cannot_be_its_own_parent(initiatives):
    """Settled by comparing two arguments, before a connection is taken.

    `initiatives_parent_not_self` says the same thing in the database and
    remains the guarantee; this only produces the better message.
    """
    one = await make_initiative(initiatives, "Ship v2")

    with pytest.raises(ValidationError) as raised:
        await initiatives.set_parent(
            scope=SCOPE,
            initiative_id=one.id,
            parent_id=one.id,
        )

    assert codes(raised) == ["SELF_PARENT"]
    assert fields(raised) == ["parentInitiativeId"]


async def test_a_two_step_cycle_is_refused(initiatives):
    """A -> B, then B -> A. The shortest loop no CHECK can see: both rows are
    in the workspace, both name a real initiative, neither names itself."""
    chain = await make_chain(initiatives, 2)

    with pytest.raises(ValidationError) as raised:
        await initiatives.set_parent(
            scope=SCOPE,
            initiative_id=chain[0].id,
            parent_id=chain[1].id,
        )

    assert codes(raised) == ["CYCLE"]


async def test_a_three_step_cycle_is_refused(initiatives):
    """A -> B -> C, then C -> A.

    Asserted separately from the two-step case rather than inferred from it: a
    walk that only ever looked at the immediate parent would pass that test and
    fail this one, and that is exactly the shape a cycle guard gets written in
    the first time.
    """
    chain = await make_chain(initiatives, 3)

    with pytest.raises(ValidationError) as raised:
        await initiatives.set_parent(
            scope=SCOPE,
            initiative_id=chain[0].id,
            parent_id=chain[2].id,
        )

    assert codes(raised) == ["CYCLE"]


async def test_nesting_a_sibling_is_not_a_cycle(initiatives):
    """The near-miss, so the guard is not simply refusing everything.

    Two initiatives under one parent, and one moved under the other. Nothing
    here is above anything it is moving under, so the walk must find nothing
    and the write must land.
    """
    root = await make_initiative(initiatives, "Ship v2")
    left = await make_initiative(initiatives, "API")
    right = await make_initiative(initiatives, "Docs")

    for child in (left, right):
        await initiatives.set_parent(
            scope=SCOPE, initiative_id=child.id, parent_id=root.id
        )

    moved = await initiatives.set_parent(
        scope=SCOPE, initiative_id=right.id, parent_id=left.id
    )

    assert moved.parent_initiative_id == left.id


# ------------------------------------------------- the hierarchy: depth


async def test_a_chain_may_reach_the_limit_exactly(initiatives):
    """The bound is inclusive, and this is what says so.

    MAX_INITIATIVE_DEPTH edges above a root, so MAX_INITIATIVE_DEPTH + 1 tiers.
    If this failed, every assertion below would be testing an off-by-one rather
    than the rule.
    """
    chain = await make_chain(initiatives, MAX_INITIATIVE_DEPTH + 1)

    assert len(chain) == MAX_INITIATIVE_DEPTH + 1

    # Read back rather than trusted from the builder: `make_chain` goes through
    # `set_parent`, so a guard that refused the last link would have raised --
    # but a guard that ACCEPTED it and wrote nothing would not, and that is the
    # failure this reload catches.
    deepest = await initiatives.get_by_id(scope=SCOPE, initiative_id=chain[-1].id)

    assert deepest is not None
    assert deepest.parent_initiative_id == chain[-2].id


async def test_one_level_past_the_limit_is_refused(initiatives):
    chain = await make_chain(initiatives, MAX_INITIATIVE_DEPTH + 1)
    extra = await make_initiative(initiatives, "One too far")

    with pytest.raises(ValidationError) as raised:
        await initiatives.set_parent(
            scope=SCOPE,
            initiative_id=extra.id,
            parent_id=chain[-1].id,
        )

    assert codes(raised) == ["TOO_DEEP"]


async def test_the_depth_rule_counts_the_whole_sub_tree_being_moved(initiatives):
    """The case a check on the parent alone would let through.

    Two chains, each comfortably within the limit on its own. Moving the root
    of the second under the leaf of the first would make one chain of their
    combined height, and every initiative in the moved sub-tree would end up
    deeper than the rule allows -- while the initiative actually named in the
    mutation is only one level down.

    This is why `inspect_parenting` walks DOWN as well as up.
    """
    half = MAX_INITIATIVE_DEPTH // 2 + 1
    left = await make_chain(initiatives, half)
    right_root = await make_initiative(initiatives, "Other root")
    right_leaf = await make_initiative(initiatives, "Other leaf")

    await initiatives.set_parent(
        scope=SCOPE, initiative_id=right_leaf.id, parent_id=right_root.id
    )

    for _ in range(half):
        deeper = await make_initiative(initiatives, "Deeper")
        await initiatives.set_parent(
            scope=SCOPE, initiative_id=deeper.id, parent_id=right_leaf.id
        )
        right_leaf = deeper

    with pytest.raises(ValidationError) as raised:
        await initiatives.set_parent(
            scope=SCOPE,
            initiative_id=right_root.id,
            parent_id=left[-1].id,
        )

    assert codes(raised) == ["TOO_DEEP"]


async def test_detaching_needs_no_guard_and_always_works(initiatives):
    """Removing an edge cannot close a loop and cannot deepen a tree, so
    `clear_parent` takes no lock and runs no walk. Asserted because "it is
    safe" is a claim about behaviour, not only about the code."""
    chain = await make_chain(initiatives, 2)

    detached = await initiatives.clear_parent(scope=SCOPE, initiative_id=chain[1].id)

    assert detached.parent_initiative_id is None


# --------------------------------------------- the hierarchy: cross-tenant


async def test_a_parent_from_another_workspace_is_a_field_error(initiatives):
    """The server's refusal, translated -- the half a unit test cannot reach.

    The other initiative is real and belongs to a real workspace, so nothing in
    this process can tell it is wrong without asking. What the client gets back
    is a field error naming `parentInitiativeId`, rather than the masked
    internal error an untranslated ForeignKeyViolationError would become.
    """
    ours = await make_initiative(initiatives, "Ours")
    theirs = await make_initiative(initiatives, "Theirs", scope=OTHER_SCOPE)

    with pytest.raises(ValidationError) as raised:
        await initiatives.set_parent(
            scope=SCOPE,
            initiative_id=ours.id,
            parent_id=theirs.id,
        )

    assert fields(raised) == ["parentInitiativeId"]
    assert codes(raised) == ["NOT_FOUND"]


async def test_an_owner_from_another_workspace_is_a_field_error(initiatives):
    with pytest.raises(ValidationError) as raised:
        await initiatives.create(
            scope=SCOPE,
            name="Ship v2",
            description=None,
            status="planned",
            target_date=None,
            owner_id=OUTSIDER_ID,
        )

    assert fields(raised) == ["ownerId"]
    assert codes(raised) == ["NOT_MEMBER"]


# ------------------------------------------------------ the project links


async def test_a_project_can_join_an_initiative_in_its_own_workspace(
    initiatives, projects
):
    initiative = await make_initiative(initiatives, "Ship v2")
    project = await make_project(projects, "API")

    joined = await initiatives.add_project(
        scope=SCOPE,
        initiative_id=initiative.id,
        project_id=project.id,
    )

    assert joined.project_ids == (project.id,)


async def test_a_project_from_another_workspace_is_a_field_error(initiatives, projects):
    """The cross-tenant refusal this whole feature is shaped around, seen from
    the layer a client talks to.

    The far project is real and belongs to a real workspace. Nothing in this
    process looked it up first, deliberately: `initiative_projects` carries one
    workspace_id read by both foreign keys, so the statement that would have
    written the row is what refuses it.
    """
    initiative = await make_initiative(initiatives, "Ship v2")
    theirs = await make_project(projects, "Theirs", scope=OTHER_SCOPE)

    with pytest.raises(ValidationError) as raised:
        await initiatives.add_project(
            scope=SCOPE,
            initiative_id=initiative.id,
            project_id=theirs.id,
        )

    assert fields(raised) == ["projectId"]
    assert codes(raised) == ["NOT_FOUND"]


async def test_an_initiative_from_another_workspace_is_a_field_error(
    initiatives, projects
):
    """The same mismatch spelled the other way round, so the constraint that
    catches it is the other one of the pair."""
    theirs = await make_initiative(initiatives, "Theirs", scope=OTHER_SCOPE)
    project = await make_project(projects, "API")

    with pytest.raises(ValidationError) as raised:
        await initiatives.add_project(
            scope=SCOPE,
            initiative_id=theirs.id,
            project_id=project.id,
        )

    assert fields(raised) == ["initiativeId"]
    assert codes(raised) == ["NOT_FOUND"]


async def test_adding_the_same_project_twice_is_a_field_error(initiatives, projects):
    initiative = await make_initiative(initiatives, "Ship v2")
    project = await make_project(projects, "API")

    await initiatives.add_project(
        scope=SCOPE, initiative_id=initiative.id, project_id=project.id
    )

    with pytest.raises(ValidationError) as raised:
        await initiatives.add_project(
            scope=SCOPE, initiative_id=initiative.id, project_id=project.id
        )

    assert codes(raised) == ["ALREADY_ASSOCIATED"]


# ------------------------------------------------------------ the updates


async def test_posting_an_update_records_history_and_stamps_the_project(projects):
    """The two writes this method exists to keep together.

    The log row is what happened; `projects.health` is what a board renders.
    They are written in one transaction precisely so no committed state has one
    without the other, and this asserts both halves rather than only the return
    value.
    """
    project = await make_project(projects, "API")

    posted = await projects.post_update(
        scope=SCOPE,
        project_id=project.id,
        health="at_risk",
        body="Slipping a week on the migration.",
        author_id=MEMBER_ID,
    )

    assert posted.health == "at_risk"

    reloaded = await projects.get_by_id(scope=SCOPE, project_id=project.id)

    assert reloaded is not None
    assert reloaded.health == "at_risk"

    history = await projects.list_updates(scope=SCOPE, project_id=project.id)

    assert [update.health for update in history] == ["at_risk"]


async def test_the_history_keeps_every_health_and_the_column_keeps_the_latest(
    projects,
):
    """ "Health over time, not just the current value", asserted as such.

    Three updates, three rows, and one column holding the last of them. If the
    column were the only storage, the first two facts would be gone.
    """
    project = await make_project(projects, "API")

    for health in ("on_track", "off_track", "at_risk"):
        await projects.post_update(
            scope=SCOPE,
            project_id=project.id,
            health=health,
            body=f"Now {health}.",
            author_id=MEMBER_ID,
        )

    history = await projects.list_updates(scope=SCOPE, project_id=project.id)

    # Newest first, which is the order the repository's index serves and the
    # order a client renders.
    assert [update.health for update in history] == [
        "at_risk",
        "off_track",
        "on_track",
    ]

    reloaded = await projects.get_by_id(scope=SCOPE, project_id=project.id)

    assert reloaded is not None
    assert reloaded.health == "at_risk"


async def test_an_update_on_another_workspaces_project_is_a_field_error(projects):
    theirs = await make_project(projects, "Theirs", scope=OTHER_SCOPE)

    with pytest.raises(ValidationError) as raised:
        await projects.post_update(
            scope=SCOPE,
            project_id=theirs.id,
            health="on_track",
            body="Looks fine from over here.",
            author_id=MEMBER_ID,
        )

    assert fields(raised) == ["projectId"]
    assert codes(raised) == ["NOT_FOUND"]


async def test_an_author_who_is_not_a_member_is_a_field_error(projects):
    """The impersonation `project_updates_author_fk` refuses.

    Reachable in one ordinary situation rather than only through a forged
    request: a session outliving the membership it was created under. The
    viewer is real and the project is real, and posting is still refused.
    """
    project = await make_project(projects, "API")

    with pytest.raises(ValidationError) as raised:
        await projects.post_update(
            scope=SCOPE,
            project_id=project.id,
            health="on_track",
            body="Not mine to report on.",
            author_id=OUTSIDER_ID,
        )

    assert fields(raised) == ["authorId"]
    assert codes(raised) == ["NOT_MEMBER"]


async def test_a_failed_update_leaves_no_health_behind(projects):
    """The transaction, seen from outside.

    An author who is not a member fails on the INSERT, which runs first
    precisely so nothing has stamped the project yet. Asserting the project's
    health is still null is what would catch a future reordering that wrote the
    column before checking who was entitled to report it.
    """
    project = await make_project(projects, "API")

    with pytest.raises(ValidationError):
        await projects.post_update(
            scope=SCOPE,
            project_id=project.id,
            health="off_track",
            body="Not mine to report on.",
            author_id=OUTSIDER_ID,
        )

    reloaded = await projects.get_by_id(scope=SCOPE, project_id=project.id)

    assert reloaded is not None
    assert reloaded.health is None


async def test_an_initiative_update_stamps_the_initiative(initiatives):
    initiative = await make_initiative(initiatives, "Ship v2")

    await initiatives.post_update(
        scope=SCOPE,
        initiative_id=initiative.id,
        health="off_track",
        body="Two of three projects are late.",
        author_id=MEMBER_ID,
    )

    reloaded = await initiatives.get_by_id(scope=SCOPE, initiative_id=initiative.id)

    assert reloaded is not None
    assert reloaded.health == "off_track"


# ------------------------------------------------------- the dependencies


async def test_a_self_dependency_is_refused_before_a_connection_is_taken(projects):
    """A comparison of two arguments: it reads no row, so there is nothing to
    race. `project_dependencies_not_self` says the same thing in the database
    and remains the guarantee."""
    project = await make_project(projects, "API")

    with pytest.raises(ValidationError) as raised:
        await projects.add_dependency(
            scope=SCOPE,
            blocking_project_id=project.id,
            blocked_project_id=project.id,
        )

    assert codes(raised) == ["SELF_DEPENDENCY"]
    assert fields(raised) == ["blockedProjectId"]


async def test_a_two_step_dependency_cycle_is_refused(projects):
    """A blocks B, then B blocks A. Both rows would be legal on their own --
    they are two distinct edges the primary key admits -- and together they are
    a deadlock nothing declarative can see."""
    first = await make_project(projects, "API")
    second = await make_project(projects, "Docs")

    await projects.add_dependency(
        scope=SCOPE,
        blocking_project_id=first.id,
        blocked_project_id=second.id,
    )

    with pytest.raises(ValidationError) as raised:
        await projects.add_dependency(
            scope=SCOPE,
            blocking_project_id=second.id,
            blocked_project_id=first.id,
        )

    assert codes(raised) == ["CYCLE"]


async def test_a_four_step_dependency_cycle_is_refused(projects):
    """A -> B -> C -> D, then D -> A.

    Deliberately longer than the initiative depth bound, because the two walks
    are bounded differently on purpose: the hierarchy's is truncated at
    MAX_INITIATIVE_DEPTH, and this one must NOT be -- a dependency chain has no
    product limit, so a bound here would silently admit exactly the long cycles
    it exists to refuse.
    """
    chain = [await make_project(projects, f"P{index}") for index in range(4)]

    # `strict=True`: the two slices are the same list offset by one, so a
    # mismatch would mean the fixture built a different number of projects
    # than it thinks -- worth failing on rather than silently truncating the
    # chain this test is about the length of.
    for blocking, blocked in zip(chain, chain[1:], strict=False):
        await projects.add_dependency(
            scope=SCOPE,
            blocking_project_id=blocking.id,
            blocked_project_id=blocked.id,
        )

    with pytest.raises(ValidationError) as raised:
        await projects.add_dependency(
            scope=SCOPE,
            blocking_project_id=chain[-1].id,
            blocked_project_id=chain[0].id,
        )

    assert codes(raised) == ["CYCLE"]


async def test_a_diamond_is_not_a_cycle(projects):
    """The near-miss: A blocks B and C, and both block D.

    D is reachable from A by two paths and nothing is reachable from D, so
    there is no loop. A guard that refused any second path to an already-seen
    project would refuse this, which is a shape real plans have.
    """
    top, left, right, bottom = [
        await make_project(projects, name) for name in ("Top", "Left", "Right", "End")
    ]

    for blocking, blocked in (
        (top, left),
        (top, right),
        (left, bottom),
    ):
        await projects.add_dependency(
            scope=SCOPE,
            blocking_project_id=blocking.id,
            blocked_project_id=blocked.id,
        )

    dependencies = await projects.add_dependency(
        scope=SCOPE,
        blocking_project_id=right.id,
        blocked_project_id=bottom.id,
    )

    assert dependencies.blocks == (bottom.id,)


async def test_a_dependency_on_another_workspaces_project_is_a_field_error(projects):
    ours = await make_project(projects, "API")
    theirs = await make_project(projects, "Theirs", scope=OTHER_SCOPE)

    with pytest.raises(ValidationError) as raised:
        await projects.add_dependency(
            scope=SCOPE,
            blocking_project_id=ours.id,
            blocked_project_id=theirs.id,
        )

    assert fields(raised) == ["blockedProjectId"]
    assert codes(raised) == ["NOT_FOUND"]


async def test_both_directions_come_back_from_one_read(projects):
    """`blockedBy` is not stored; it is this row read from the other end.

    Asserted from the middle project of a chain, which is the only position
    where both lists are non-empty and a mixed-up direction would be visible.
    """
    first, middle, last = [
        await make_project(projects, name) for name in ("First", "Middle", "Last")
    ]

    for blocking, blocked in ((first, middle), (middle, last)):
        await projects.add_dependency(
            scope=SCOPE,
            blocking_project_id=blocking.id,
            blocked_project_id=blocked.id,
        )

    found = await projects.list_dependencies_for_projects(
        scope=SCOPE, project_ids=[middle.id]
    )

    assert found[middle.id].blocks == (last.id,)
    assert found[middle.id].blocked_by == (first.id,)


async def test_removing_a_dependency_that_was_never_there_is_not_an_error(projects):
    """The caller's intent -- this project does not block that one -- already
    holds, so reporting a failure would be reporting on the id rather than on
    the request. Deleting a relation by id is a different case and IS reported;
    see RelationService.delete_relation."""
    first = await make_project(projects, "API")
    second = await make_project(projects, "Docs")

    dependencies = await projects.remove_dependency(
        scope=SCOPE,
        blocking_project_id=first.id,
        blocked_project_id=second.id,
    )

    assert dependencies.blocks == ()


# ------------------------------------------------------------- deletion


async def test_deleting_a_project_detaches_it_from_its_initiatives(
    initiatives, projects
):
    """RESTRICT means the service has to do this itself, in order, and the
    order is what this asserts: the link rows are gone and the initiative is
    still there."""
    initiative = await make_initiative(initiatives, "Ship v2")
    project = await make_project(projects, "API")

    await initiatives.add_project(
        scope=SCOPE, initiative_id=initiative.id, project_id=project.id
    )

    await projects.delete(scope=SCOPE, project_id=project.id)

    reloaded = await initiatives.get_by_id(scope=SCOPE, initiative_id=initiative.id)

    assert reloaded is not None
    assert reloaded.project_ids == ()


async def test_deleting_an_initiative_promotes_its_children(initiatives):
    """The product decision `initiatives_parent_fk`'s RESTRICT forces somebody
    to make out loud: deleting a goal must not destroy the goals beneath it."""
    chain = await make_chain(initiatives, 2)

    await initiatives.delete(scope=SCOPE, initiative_id=chain[0].id)

    survivor = await initiatives.get_by_id(scope=SCOPE, initiative_id=chain[1].id)

    assert survivor is not None
    assert survivor.parent_initiative_id is None


async def test_deleting_a_project_that_blocks_another_clears_both_directions(
    projects,
):
    """A project can be blocking and blocked at once, so the delete has to
    clear both ends -- clearing one at a time would leave the second statement
    to fail on the half already gone."""
    first, middle, last = [
        await make_project(projects, name) for name in ("First", "Middle", "Last")
    ]

    for blocking, blocked in ((first, middle), (middle, last)):
        await projects.add_dependency(
            scope=SCOPE,
            blocking_project_id=blocking.id,
            blocked_project_id=blocked.id,
        )

    await projects.delete(scope=SCOPE, project_id=middle.id)

    found = await projects.list_dependencies_for_projects(
        scope=SCOPE, project_ids=[first.id, last.id]
    )

    assert found[first.id].blocks == ()
    assert found[last.id].blocked_by == ()


# ------------------------------------------------------------ isolation


async def test_a_workspace_only_ever_lists_its_own_initiatives(initiatives):
    """The tenant predicate on the list, asserted with a real neighbour rather
    than an empty database -- which would pass whatever the predicate said."""
    await make_initiative(initiatives, "Ours")
    await make_initiative(initiatives, "Theirs", scope=OTHER_SCOPE)

    page = await initiatives.list(scope=SCOPE, first=50, after=None)

    assert [node.name for node in page.nodes] == ["Ours"]


async def test_an_initiative_from_another_workspace_reads_as_absent(initiatives):
    """ "Not in this workspace" and "does not exist" are one answer, so a caller
    holding a guessed or leaked id learns nothing by asking."""
    theirs = await make_initiative(initiatives, "Theirs", scope=OTHER_SCOPE)

    assert await initiatives.get_by_id(scope=SCOPE, initiative_id=theirs.id) is None
