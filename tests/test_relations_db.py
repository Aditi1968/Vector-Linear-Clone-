"""Sub-issues and relations against a real PostgreSQL 18.

tests/test_relations.py asserts the pure rules -- canonicalisation, the
vocabulary, the two refusals settled before a connection. None of that can
prove the thing this feature is actually risky for, because a relation row
names *two* issues and every guarantee about them is a constraint. A fake
connection agrees with whatever it is handed; only a server refuses.

Four claims, in the order they would hurt:

  * A relation cannot join two workspaces. This is the leak the table is
    shaped to make impossible -- both foreign keys are composite onto
    `issues (workspace_id, id)`, so a cross-tenant edge has no matching row
    rather than being caught by application code that might one day be
    skipped. Asserted from both ends, because a single-column key on either
    side alone would still pass one of the two.
  * A relationship has one row however it is said. `(A, B, related)` and
    `(B, A, related)` are the same edge, and the second insert is refused --
    which is only true because `_canonical` orders symmetric pairs and
    `issue_relations_symmetric_ordered` makes the unordered form
    unrepresentable.
  * A sub-issue may cross teams and may not cross workspaces. Both halves
    come out of one foreign key's column list, so the permissive half is
    asserted as carefully as the restrictive one -- adding `team_id` to
    `issues_parent_fk` would be a one-word change that silently outlaws a
    product rule, and only a test naming that case would notice.
  * The cycle guard refuses loops at any depth. It is the one rule no
    constraint can express; see `test_a_cycle_deeper_than_one_level_is_refused`
    and the honesty note above it.

The seed is deliberately two tenants at once. A suite with one workspace can
assert that a legal write succeeds and can never assert that an illegal one
is refused, because there is nothing for it to be illegal against.

Marked `db`: deselected by default, skipped when Docker is unreachable.
Nothing here touches DATABASE_URL or Neon; the only server it speaks to is
the throwaway container `postgres_dsn` starts.
"""

from dataclasses import dataclass
from uuid import UUID

import asyncpg
import pytest

from app.domain.errors import ValidationError
from app.domain.relations import RelationType
from app.domain.tenancy import WorkspaceScope
from app.repositories.relations import RelationRepository
from app.services.relations import RelationService

from tests.conftest import (
    apply_all_migrations,
    reset_schema,
    seed_workflow_states,
)


pytestmark = pytest.mark.db

# Workspace A and its first team are the rows 002 seeds, named as literals so
# a test asserts against a constant rather than querying for the value it is
# about to check.
WORKSPACE_A = UUID("00000000-0000-7000-8000-000000000001")
TEAM_A = UUID("00000000-0000-7000-8000-000000000002")

# A second team inside workspace A. Its only purpose is the cross-team
# sub-issue rule: without it, "a sub-issue may belong to another team" is not
# a statement this database can be asked about.
TEAM_A2 = UUID("00000000-0000-7000-8000-0000000000a2")

WORKSPACE_B = UUID("00000000-0000-7000-8000-0000000000e1")
TEAM_B = UUID("00000000-0000-7000-8000-0000000000e2")

SCOPE_A = WorkspaceScope(workspace_id=WORKSPACE_A)
SCOPE_B = WorkspaceScope(workspace_id=WORKSPACE_B)


def _issue_id(suffix: int) -> UUID:
    """A UUID whose byte order follows `suffix`.

    PostgreSQL compares `uuid` bytewise, so the canonical-ordering
    assertions below need ids whose relative order a reader can see. Every id
    shares its leading ten bytes and the trailing six are decimal digits, so
    hex order is `suffix` order.
    """
    return UUID(f"a1b2c3d4-0000-4000-8000-{suffix:012d}")


# Three issues in workspace A on its first team, one on its second team, and
# one in workspace B. A1 < A2 < A3 by id, which is what makes the symmetric
# canonicalisation observable rather than coincidental.
A1 = _issue_id(10)
A2 = _issue_id(20)
A3 = _issue_id(30)
A_OTHER_TEAM = _issue_id(40)
B1 = _issue_id(50)

# An id belonging to no workspace at all. Every assertion that uses it is
# really a comparison: a client naming this must learn exactly what a client
# naming another tenant's real id learns.
ABSENT = _issue_id(999)

INSERT_ISSUE = """
    INSERT INTO issues (
        id, workspace_id, team_id, number, workflow_state_id, title, priority
    )
    VALUES (
        $1, $2, $3, $4,
        (
            SELECT id
            FROM workflow_states
            WHERE workspace_id = $2 AND team_id = $3 AND type = 'unstarted'
            ORDER BY position, id
            LIMIT 1
        ),
        $5, 0
    )
"""

SEED = (
    (A1, WORKSPACE_A, TEAM_A, 1, "a one"),
    (A2, WORKSPACE_A, TEAM_A, 2, "a two"),
    (A3, WORKSPACE_A, TEAM_A, 3, "a three"),
    (A_OTHER_TEAM, WORKSPACE_A, TEAM_A2, 1, "a other team"),
    (B1, WORKSPACE_B, TEAM_B, 1, "b one"),
)


@dataclass(frozen=True, slots=True)
class Related:
    service: RelationService
    connection: asyncpg.Connection


@pytest.fixture
async def related(postgres_dsn):
    """Every migration through the runner, two tenants, three teams, a pool.

    The schema is built from the migrations rather than from hand-written
    DDL, because the claims under test are about the constraints those files
    declare -- a test that created its own tables would be asserting against
    a second definition of the schema that nothing keeps in step.

    The raw connection is yielded alongside the service so that two tests can
    write SQL the repository deliberately never sends: the CHECK constraints
    are only reachable by going around the service that refuses first.
    """
    connection = await asyncpg.connect(postgres_dsn)

    try:
        await reset_schema(connection)
        await apply_all_migrations(connection)

        await connection.execute(
            "INSERT INTO workspaces (id, slug, name) VALUES ($1, $2, $3)",
            WORKSPACE_B,
            "acme",
            "Acme",
        )

        await connection.executemany(
            "INSERT INTO teams (id, workspace_id, name, key) VALUES ($1, $2, $3, $4)",
            [
                (TEAM_A2, WORKSPACE_A, "Design", "DES"),
                (TEAM_B, WORKSPACE_B, "Acme Core", "ACME"),
            ],
        )

        # 005 seeded boards for the teams that existed when it ran. Both of
        # these were created after it, so neither has one, and an issue
        # cannot be filed against a team with no workflow state.
        await seed_workflow_states(connection, WORKSPACE_A, TEAM_A2)
        await seed_workflow_states(connection, WORKSPACE_B, TEAM_B)

        await connection.executemany(INSERT_ISSUE, SEED)

        pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)

        try:
            yield Related(
                service=RelationService(pool=pool, repository=RelationRepository()),
                connection=connection,
            )
        finally:
            await pool.close()
    finally:
        await connection.close()


def _codes(error: ValidationError) -> list[tuple[str, str]]:
    return [(issue.field, issue.code) for issue in error.issues]


# --- a relation cannot join two workspaces -----------------------------


async def test_relating_across_workspaces_is_refused_from_the_source_side(related):
    """Workspace A cannot relate its issue to workspace B's.

    Refused by `issue_relations_target_fk`: the relation carries workspace A,
    so the pair (A, B1) matches no row in `issues (workspace_id, id)`. No
    SELECT ran first -- the constraint is the check, and a pre-check would be
    a second copy of it with a window in the middle.
    """
    with pytest.raises(ValidationError) as raised:
        await related.service.create_relation(
            scope=SCOPE_A,
            source_issue_id=A1,
            target_issue_id=B1,
            relation_type=RelationType.BLOCKS,
        )

    assert _codes(raised.value) == [("targetIssueId", "NOT_FOUND")]


async def test_relating_across_workspaces_is_refused_from_the_target_side(related):
    """The mirror image, which a single-column foreign key would have allowed.

    Asserted separately rather than folded into the test above because the
    two exercise different keys. A schema that carried
    `REFERENCES issues(id)` on one side and the composite on the other would
    pass one of these two and leak through the other.
    """
    with pytest.raises(ValidationError) as raised:
        await related.service.create_relation(
            scope=SCOPE_A,
            source_issue_id=B1,
            target_issue_id=A1,
            relation_type=RelationType.BLOCKS,
        )

    assert _codes(raised.value) == [("sourceIssueId", "NOT_FOUND")]


async def test_another_tenants_issue_is_refused_exactly_as_one_that_never_existed(
    related,
):
    """The property that stops the mutation being an existence oracle.

    B1 is a real row; ABSENT is not. If the two answers differed in field,
    code or message, a caller holding a guessed id could ask this mutation
    whether it names somebody's issue.
    """
    errors = []

    for target in (B1, ABSENT):
        with pytest.raises(ValidationError) as raised:
            await related.service.create_relation(
                scope=SCOPE_A,
                source_issue_id=A1,
                target_issue_id=target,
                relation_type=RelationType.RELATED,
            )

        errors.append(
            [(issue.field, issue.code, issue.message) for issue in raised.value.issues]
        )

    assert errors[0] == errors[1]


async def test_a_relation_in_another_workspace_cannot_be_deleted(related):
    """Deleting is scoped by workspace in the predicate, not checked after.

    Workspace B's relation is invisible to workspace A, so A deleting it by
    id is a miss rather than a success -- and the row survives, which is the
    half that a `DELETE ... RETURNING` filtering afterwards would fail.
    """
    # Workspace B holds one seeded issue, and a relation needs two. The
    # second is created here rather than in the seed because this is the
    # only test that needs it.
    b2 = _issue_id(51)
    await related.connection.execute(INSERT_ISSUE, b2, WORKSPACE_B, TEAM_B, 2, "b two")

    relation = await related.service.create_relation(
        scope=SCOPE_B,
        source_issue_id=B1,
        target_issue_id=b2,
        relation_type=RelationType.RELATED,
    )

    with pytest.raises(ValidationError) as raised:
        await related.service.delete_relation(scope=SCOPE_A, relation_id=relation.id)

    assert _codes(raised.value) == [("id", "NOT_FOUND")]

    survived = await related.connection.fetchval(
        "SELECT count(*) FROM issue_relations WHERE id = $1", relation.id
    )

    assert survived == 1


# --- one row per relationship ------------------------------------------


@pytest.mark.parametrize(
    "relation_type", [RelationType.RELATED, RelationType.DUPLICATE]
)
async def test_a_symmetric_relation_said_the_other_way_round_is_a_duplicate(
    related, relation_type
):
    """The claim the canonical ordering exists to make true.

    Without `_canonical` reordering the pair, (A2, A1, related) is a
    different tuple from (A1, A2, related) and `issue_relations_unique`
    accepts both -- leaving one relationship stored twice, deletable once,
    and displayed twice. This is that constraint doing what it reads as
    doing.
    """
    await related.service.create_relation(
        scope=SCOPE_A,
        source_issue_id=A1,
        target_issue_id=A2,
        relation_type=relation_type,
    )

    with pytest.raises(ValidationError) as raised:
        await related.service.create_relation(
            scope=SCOPE_A,
            source_issue_id=A2,
            target_issue_id=A1,
            relation_type=relation_type,
        )

    assert _codes(raised.value) == [("type", "DUPLICATE")]

    stored = await related.connection.fetchval(
        "SELECT count(*) FROM issue_relations WHERE workspace_id = $1", WORKSPACE_A
    )

    assert stored == 1


async def test_blocked_by_and_blocks_are_the_same_stored_edge(related):
    """`A blocked_by B` and `B blocks A` are one relationship, so one row.

    The second call is refused as a duplicate, which is only correct because
    `_canonical` turned the `blocked_by` into a `blocks` with its ends
    exchanged rather than storing a fourth value.
    """
    await related.service.create_relation(
        scope=SCOPE_A,
        source_issue_id=A1,
        target_issue_id=A2,
        relation_type=RelationType.BLOCKED_BY,
    )

    row = await related.connection.fetchrow(
        "SELECT source_issue_id, target_issue_id, type FROM issue_relations"
    )

    assert (row["source_issue_id"], row["target_issue_id"], row["type"]) == (
        A2,
        A1,
        "blocks",
    )

    with pytest.raises(ValidationError) as raised:
        await related.service.create_relation(
            scope=SCOPE_A,
            source_issue_id=A2,
            target_issue_id=A1,
            relation_type=RelationType.BLOCKS,
        )

    assert _codes(raised.value) == [("type", "DUPLICATE")]


async def test_blocking_is_allowed_in_both_directions_between_one_pair(related):
    """Mutual blocking is a product judgement, not an integrity violation.

    `issue_relations_symmetric_ordered` exempts 'blocks' precisely so this
    is representable. It is probably a mistake in a workflow and the schema
    does not refuse it -- asserted so that a future "tidy-up" that orders
    every type notices it is changing behaviour.
    """
    await related.service.create_relation(
        scope=SCOPE_A,
        source_issue_id=A1,
        target_issue_id=A2,
        relation_type=RelationType.BLOCKS,
    )
    await related.service.create_relation(
        scope=SCOPE_A,
        source_issue_id=A2,
        target_issue_id=A1,
        relation_type=RelationType.BLOCKS,
    )

    stored = await related.connection.fetchval("SELECT count(*) FROM issue_relations")

    assert stored == 2


async def test_one_pair_may_carry_several_kinds_of_relation_at_once(related):
    """`type` is part of the unique key, so it does not cap a pair at one edge."""
    for relation_type in (RelationType.BLOCKS, RelationType.RELATED):
        await related.service.create_relation(
            scope=SCOPE_A,
            source_issue_id=A1,
            target_issue_id=A2,
            relation_type=relation_type,
        )

    stored = await related.connection.fetchval("SELECT count(*) FROM issue_relations")

    assert stored == 2


async def test_the_server_refuses_a_self_relation_even_with_the_service_bypassed(
    related,
):
    """`issue_relations_not_self` is the guarantee; the service is the message.

    The service refuses before a connection is taken, so this goes around it
    with raw SQL. Otherwise the constraint is untested and the rule rests
    entirely on an `if` that a second write path would not inherit.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await related.connection.execute(
            """
            INSERT INTO issue_relations
                (workspace_id, source_issue_id, target_issue_id, type)
            VALUES ($1, $2, $2, 'related')
            """,
            WORKSPACE_A,
            A1,
        )

    assert raised.value.constraint_name == "issue_relations_not_self"


async def test_the_server_refuses_an_unordered_symmetric_pair(related):
    """The constraint that makes the canonical form the only representable one.

    Written straight to the table in the order `_canonical` would have
    reversed. If this ever succeeds, the uniqueness argument above is void:
    both orders become storable and the duplicate test passes only because
    the application happens to normalise.
    """
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await related.connection.execute(
            """
            INSERT INTO issue_relations
                (workspace_id, source_issue_id, target_issue_id, type)
            VALUES ($1, $2, $3, 'related')
            """,
            WORKSPACE_A,
            A2,
            A1,
        )

    assert raised.value.constraint_name == "issue_relations_symmetric_ordered"


async def test_the_server_refuses_a_relation_type_outside_the_vocabulary(related):
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await related.connection.execute(
            """
            INSERT INTO issue_relations
                (workspace_id, source_issue_id, target_issue_id, type)
            VALUES ($1, $2, $3, 'blocked_by')
            """,
            WORKSPACE_A,
            A1,
            A2,
        )

    assert raised.value.constraint_name == "issue_relations_type_known"


# --- reading a relation from both ends ---------------------------------


async def test_one_stored_row_is_read_under_both_of_its_names(related):
    """A `blocks` row is a BLOCKED_BY to the issue at its far end.

    This is the whole payoff of storing one row instead of two: both
    readings come from the same id, so deleting through either end removes
    the relationship for both, and neither side can drift from the other
    because there is no other side to drift from.
    """
    created = await related.service.create_relation(
        scope=SCOPE_A,
        source_issue_id=A1,
        target_issue_id=A2,
        relation_type=RelationType.BLOCKS,
    )

    from_source = await related.service.list_relations(
        scope=SCOPE_A, issue_id=A1, first=10, after=None
    )
    from_target = await related.service.list_relations(
        scope=SCOPE_A, issue_id=A2, first=10, after=None
    )

    assert [(node.id, node.type, node.issue.id) for node in from_source.nodes] == [
        (created.id, RelationType.BLOCKS, A2)
    ]
    assert [(node.id, node.type, node.issue.id) for node in from_target.nodes] == [
        (created.id, RelationType.BLOCKED_BY, A1)
    ]


async def test_a_relation_page_stays_inside_its_workspace(related):
    """Workspace B sees none of workspace A's relations, and vice versa."""
    await related.service.create_relation(
        scope=SCOPE_A,
        source_issue_id=A1,
        target_issue_id=A2,
        relation_type=RelationType.RELATED,
    )

    page = await related.service.list_relations(
        scope=SCOPE_B, issue_id=A1, first=10, after=None
    )

    assert page.nodes == []


async def test_a_relation_page_walks_without_repeating_or_skipping(related):
    """The keyset cursor over a UNION of both directions.

    A1 is given three relations, then read one at a time. The union is where
    a cursor is easy to get wrong -- the two halves are ordered
    independently and merged -- so a page walk that dropped or repeated a
    row would show up here and nowhere else.
    """
    for target in (A2, A3, A_OTHER_TEAM):
        await related.service.create_relation(
            scope=SCOPE_A,
            source_issue_id=target,
            target_issue_id=A1,
            relation_type=RelationType.BLOCKS,
        )

    seen = []
    cursor = None

    while True:
        page = await related.service.list_relations(
            scope=SCOPE_A, issue_id=A1, first=1, after=cursor
        )
        seen.extend(node.id for node in page.nodes)

        if not page.has_next_page:
            break

        cursor = page.end_cursor

    assert len(seen) == 3
    assert len(set(seen)) == 3


# --- sub-issues --------------------------------------------------------


async def test_a_sub_issue_may_belong_to_another_team_in_the_same_workspace(related):
    """The permissive half of `issues_parent_fk`, asserted on purpose.

    The key names `(workspace_id, parent_id)` and not the team. Adding
    `team_id` to that list would be a one-word change that reads like
    tightening a constraint and actually deletes a product rule, and this is
    the test that would fail rather than nothing failing at all.
    """
    child = await related.service.set_parent(
        scope=SCOPE_A, issue_id=A_OTHER_TEAM, parent_id=A1
    )

    assert child.id == A_OTHER_TEAM

    parent = await related.service.find_parent(scope=SCOPE_A, issue_id=A_OTHER_TEAM)

    assert parent is not None
    assert parent.id == A1


async def test_a_parent_in_another_workspace_is_refused(related):
    """The restrictive half of the same key, from the same column list."""
    with pytest.raises(ValidationError) as raised:
        await related.service.set_parent(scope=SCOPE_A, issue_id=A1, parent_id=B1)

    assert _codes(raised.value) == [("parentId", "NOT_FOUND")]


async def test_setting_a_parent_on_another_workspaces_issue_changes_nothing(related):
    """The UPDATE is scoped by workspace, so it matches no row rather than one.

    Reported as a miss on the issue rather than on the parent, and the row
    in workspace B is left alone -- which is what separates "the predicate
    excluded it" from "it was found and then rejected".
    """
    with pytest.raises(ValidationError) as raised:
        await related.service.set_parent(scope=SCOPE_A, issue_id=B1, parent_id=A1)

    assert _codes(raised.value) == [("issueId", "NOT_FOUND")]

    untouched = await related.connection.fetchval(
        "SELECT parent_id FROM issues WHERE id = $1", B1
    )

    assert untouched is None


async def test_a_direct_cycle_is_refused(related):
    """A is B's parent, so B may not become A's."""
    await related.service.set_parent(scope=SCOPE_A, issue_id=A2, parent_id=A1)

    with pytest.raises(ValidationError) as raised:
        await related.service.set_parent(scope=SCOPE_A, issue_id=A1, parent_id=A2)

    assert _codes(raised.value) == [("parentId", "CYCLE")]


async def test_a_cycle_deeper_than_one_level_is_refused(related):
    """A -> B -> C, so C may not become A's parent.

    The case no CHECK constraint can see: every row involved names a real
    issue in the right workspace and none names itself, so the database
    accepts all three writes individually. The guard is the recursive walk
    in `RelationService.set_parent` under a per-workspace advisory lock, and
    it is only sound for writers that take that lock -- a bulk import or a
    hand-run UPDATE can still write this cycle. That is a documented
    limitation of the service, not a property of the schema.
    """
    await related.service.set_parent(scope=SCOPE_A, issue_id=A2, parent_id=A1)
    await related.service.set_parent(scope=SCOPE_A, issue_id=A3, parent_id=A2)

    with pytest.raises(ValidationError) as raised:
        await related.service.set_parent(scope=SCOPE_A, issue_id=A1, parent_id=A3)

    assert _codes(raised.value) == [("parentId", "CYCLE")]

    # The refusal rolled back cleanly rather than leaving a half-written edge.
    assert (
        await related.connection.fetchval(
            "SELECT parent_id FROM issues WHERE id = $1", A1
        )
        is None
    )


async def test_the_server_refuses_a_self_parent_even_with_the_service_bypassed(related):
    """`issues_parent_not_self` is the guarantee behind the service's message."""
    with pytest.raises(asyncpg.CheckViolationError) as raised:
        await related.connection.execute(
            "UPDATE issues SET parent_id = $1 WHERE id = $1", A1
        )

    assert raised.value.constraint_name == "issues_parent_not_self"


async def test_clearing_a_parent_an_issue_does_not_have_succeeds(related):
    """The caller asked for a state and the state holds, so it is not an error.

    Deliberately different from deleting a relation by id, which reports a
    miss: an id names a specific row, so "it was not there" is information
    about what the client sent.
    """
    cleared = await related.service.clear_parent(scope=SCOPE_A, issue_id=A1)

    assert cleared.id == A1

    again = await related.service.clear_parent(scope=SCOPE_A, issue_id=A1)

    assert again.id == A1


async def test_children_are_listed_without_a_team_filter(related):
    """A parent's sub-issues include the ones on other teams of its workspace."""
    await related.service.set_parent(scope=SCOPE_A, issue_id=A2, parent_id=A1)
    await related.service.set_parent(scope=SCOPE_A, issue_id=A_OTHER_TEAM, parent_id=A1)

    page = await related.service.list_children(
        scope=SCOPE_A, parent_id=A1, first=10, after=None
    )

    assert {node.id for node in page.nodes} == {A2, A_OTHER_TEAM}


async def test_children_of_another_workspaces_issue_are_an_empty_page(related):
    """Not an error: a total field, and one that cannot confirm an id is real."""
    await related.service.set_parent(scope=SCOPE_A, issue_id=A2, parent_id=A1)

    page = await related.service.list_children(
        scope=SCOPE_B, parent_id=A1, first=10, after=None
    )

    assert page.nodes == []
    assert page.has_next_page is False


async def test_deleting_an_issue_that_is_a_parent_is_refused_rather_than_cascading(
    related,
):
    """ON DELETE RESTRICT, so no delete silently takes a sub-tree with it.

    There is no issue-deletion path in the product yet. This asserts what
    the schema would do if one were added carelessly, so that whoever adds
    it has to choose between refusing, re-parenting and orphaning rather
    than inheriting a guess.

    `RestrictViolationError`, not `ForeignKeyViolationError`. The two are
    siblings under `IntegrityConstraintViolationError` rather than one being
    a subclass of the other, and they arrive from opposite sides of the same
    key: a bad INSERT or UPDATE of the *referencing* row raises the latter --
    which is what `RelationRepository` catches -- while RESTRICT firing on
    the *referenced* row raises this one. Whoever writes the deletion path
    will be catching a different exception than the one already handled.
    """
    await related.service.set_parent(scope=SCOPE_A, issue_id=A2, parent_id=A1)

    with pytest.raises(asyncpg.RestrictViolationError):
        await related.connection.execute("DELETE FROM issues WHERE id = $1", A1)
