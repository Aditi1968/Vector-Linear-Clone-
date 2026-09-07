"""Triage and label groups, without a database.

What is asserted here is what does not need a server to be true: the shape of
the statements, the arguments that reach them, the validation that happens
before a connection is taken, and the composition of the schema. The rules that
are the DATABASE's -- exclusivity, the cascade, tenancy through composite keys
-- are in tests/test_migration_021_db.py, where they are exercised against
PostgreSQL rather than described.

The one thing worth stating about the split: nothing in this file asserts that
a cross-tenant write is refused. It could only assert that the service passes
the workspace along, which is a weaker claim than the schema's and would read
as the stronger one. The scope arguments ARE checked here, because a statement
that forgot to carry one is a defect this layer can see.
"""

import inspect
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.domain.errors import ValidationError
from app.domain.labels import LabelEntity, LabelGroupEntity
from app.domain.triage import TriageIssueEntity, TriageIssuePage
from app.graphql.schema import build_schema
from app.repositories.issue_labels import IssueLabelRepository
from app.repositories.label_groups import LabelGroupRepository
from app.repositories.labels import LabelRepository
from app.repositories.triage import TriageRepository
from app.services.labels import (
    DEFAULT_GROUP_EXCLUSIVE,
    GROUPS_PER_WORKSPACE_MAX,
    NAME_MAX_LENGTH,
    LabelService,
)
from app.services.triage import FIRST_MAX, TriageService

from tests.conftest import (
    TEST_SCOPE,
    TEST_TEAM_ID,
    ExplodingPool,
    FakeConnection,
    FakePool,
    make_entity,
    normalize,
)


GROUP_ID = UUID("00000000-0000-7000-8000-0000000000c1")
LABEL_ID = UUID("00000000-0000-7000-8000-0000000000c2")
ISSUE_ID = UUID("00000000-0000-7000-8000-0000000000c3")
STATE_ID = UUID("00000000-0000-7000-8000-0000000000c4")

CREATED_AT = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


def group_row(name="Status", exclusive=True) -> dict:
    """asyncpg.Record supports __getitem__, which a dict models well enough."""
    return {
        "id": GROUP_ID,
        "name": name,
        "exclusive": exclusive,
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }


def label_row(group_id=None) -> dict:
    return {
        "id": LABEL_ID,
        "name": "bug",
        "color": "#6b7280",
        "group_id": group_id,
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }


class SequencedValueConnection(FakeConnection):
    """A FakeConnection whose `fetchval` answers each query in turn.

    `attach` issues two of them -- the per-issue label count, then the insert
    -- and one canned value for both makes the count read as the insert's
    result. A fake that cannot tell them apart reports itself rather than the
    behaviour.
    """

    def __init__(self, values):
        super().__init__()

        self._values = list(values)

    async def fetchval(self, query, *args):
        self.queries.append({"query": query, "args": args})

        return self._values.pop(0) if self._values else None


def label_service(pool) -> LabelService:
    return LabelService(
        pool=pool,
        repository=LabelRepository(),
        issue_label_repository=IssueLabelRepository(),
        group_repository=LabelGroupRepository(),
    )


def triage_service(pool, teams=None) -> TriageService:
    return TriageService(
        pool=pool,
        repository=TriageRepository(),
        # Neither collaborator is reached in this file: the duplicate path
        # needs a relation repository over a real connection, and the team
        # change needs a team service that can allocate a number. Both are
        # exercised in the db suite; here they are placeholders that would fail
        # loudly if a test reached one by accident.
        relations=None,
        teams=teams,
    )


def codes(exc: ValidationError) -> list[str]:
    return [issue.code for issue in exc.issues]


# --- label groups: validation before a connection ----------------------


async def test_a_group_name_at_the_ceiling_is_accepted(
    exploding_pool: ExplodingPool,
):
    """The boundary from the accepting side.

    `ExplodingPool` raises on acquire, so an AssertionError here means the name
    was accepted and the service went looking for a connection -- which is how
    "validated first" is asserted rather than assumed.
    """
    with pytest.raises(AssertionError):
        await label_service(exploding_pool).create_group(
            scope=TEST_SCOPE,
            name="x" * NAME_MAX_LENGTH,
            exclusive=True,
        )


@pytest.mark.parametrize(
    ("name", "code"),
    [("", "REQUIRED"), ("x" * (NAME_MAX_LENGTH + 1), "TOO_LONG")],
)
async def test_an_invalid_group_name_never_reaches_the_pool(
    exploding_pool: ExplodingPool, name, code
):
    """The same two numbers a label name is checked against.

    Shared bounds rather than a second pair, because a group name and a label
    name are rendered in the same picker and a limit that differed between them
    would be a rule nobody could state.
    """
    with pytest.raises(ValidationError) as raised:
        await label_service(exploding_pool).create_group(
            scope=TEST_SCOPE, name=name, exclusive=False
        )

    assert codes(raised.value) == [code]
    assert exploding_pool.acquire_count == 0


async def test_an_unchosen_exclusivity_becomes_the_services_default():
    """Migration 021 gives `exclusive` no database default, deliberately: the
    two answers are not interchangeable, and a default in the schema would
    outlive the migration and decide for every insert that forgot. The
    application's default lives in one named constant a reader can find."""
    connection = FakeConnection(row=group_row(exclusive=DEFAULT_GROUP_EXCLUSIVE))
    pool = FakePool(connection)

    await label_service(pool).create_group(
        scope=TEST_SCOPE, name="Area", exclusive=None
    )

    assert connection.queries[0]["args"][2] is DEFAULT_GROUP_EXCLUSIVE


async def test_the_default_is_the_permissive_one():
    """A plain group only nests labels; an exclusive one refuses writes. The
    safe default is the one that takes nothing away."""
    assert DEFAULT_GROUP_EXCLUSIVE is False


async def test_a_group_list_is_bounded_rather_than_paginated():
    """Not paginated, because a group is an AXIS a team classifies work along
    and the list is the size of a picker. Bounded anyway, because "a handful"
    is an expectation and not a constraint -- without it this is an unpaginated
    field the complexity rule charges as one."""
    connection = FakeConnection(rows=[group_row()])
    pool = FakePool(connection)

    await label_service(pool).list_groups(scope=TEST_SCOPE)

    issued = connection.queries[0]

    assert issued["args"] == (TEST_SCOPE.workspace_id, GROUPS_PER_WORKSPACE_MAX)
    assert "LIMIT $2" in normalize(issued["query"])


# --- label groups: the statements ---------------------------------------


async def test_deleting_a_group_ungroups_its_labels_first():
    """`labels_group_fk` is ON DELETE RESTRICT, so the order is not a
    preference: the delete fails outright while any label is still in the
    group. Both statements are in one transaction, which is what makes the
    constraint a guard on that ordering rather than an obstacle to it."""
    connection = FakeConnection(value=GROUP_ID)
    pool = FakePool(connection)

    await label_service(pool).delete_group(scope=TEST_SCOPE, group_id=GROUP_ID)

    issued = [normalize(query["query"]) for query in connection.queries]

    assert issued[0].startswith("UPDATE labels SET group_id = NULL")
    assert issued[1].startswith("DELETE FROM label_groups")


async def test_ungrouping_clears_both_columns_together():
    """`labels_group_exclusive_paired` refuses a row where one is NULL and the
    other is not, so clearing only `group_id` is a state the server would
    reject rather than merely one that would look odd."""
    connection = FakeConnection(value=GROUP_ID)
    pool = FakePool(connection)

    await label_service(pool).delete_group(scope=TEST_SCOPE, group_id=GROUP_ID)

    ungroup = normalize(connection.queries[0]["query"])

    assert "group_id = NULL" in ungroup
    assert "group_exclusive = NULL" in ungroup


async def test_setting_a_group_reads_its_exclusivity_in_the_same_statement():
    """The label's copy of `exclusive` is written from a SUBQUERY, never from a
    parameter.

    That is the whole design of `set_group`: the only correct source for the
    copy is the group's own row, and reading it inside the statement that
    writes it means there is no round trip during which the group could be
    flipped. A parameter here would be a value a caller could simply assert.
    """
    connection = FakeConnection(row=label_row(group_id=GROUP_ID))
    pool = FakePool(connection)

    await label_service(pool).set_label_group(
        scope=TEST_SCOPE, label_id=LABEL_ID, group_id=GROUP_ID
    )

    issued = connection.queries[0]
    sql = normalize(issued["query"])

    assert "group_exclusive = ( SELECT label_groups.exclusive" in sql
    assert "label_groups.workspace_id = $1" in sql
    # Three parameters: the workspace, the label and the group. A fourth would
    # be the exclusivity arriving from outside.
    assert len(issued["args"]) == 3


async def test_a_missing_label_is_reported_rather_than_silently_ignored():
    """The UPDATE matching no row is the only signal there is, and it covers a
    label in another workspace and an id that exists nowhere alike -- so the
    two stay indistinguishable, which is what keeps the mutation from being an
    existence oracle."""
    connection = FakeConnection(row=None)
    pool = FakePool(connection)

    with pytest.raises(ValidationError) as raised:
        await label_service(pool).set_label_group(
            scope=TEST_SCOPE, label_id=LABEL_ID, group_id=None
        )

    assert codes(raised.value) == ["NOT_FOUND"]


async def test_attaching_reads_the_labels_exclusivity_key_from_the_label():
    """`exclusivity_key` is NOT NULL on `issue_labels` and pinned to the label
    by a composite foreign key, so an attach must supply the label's own value.

    Reading it in the INSERT ... SELECT is what closes the window: a value read
    first and written second could be invalidated by a regroup in between, and
    a value supplied by the caller could be anything at all.
    """
    connection = SequencedValueConnection([0, LABEL_ID])
    pool = FakePool(connection)

    await label_service(pool).attach(
        scope=TEST_SCOPE, issue_id=ISSUE_ID, label_id=LABEL_ID
    )

    attach = next(
        normalize(query["query"])
        for query in connection.queries
        if "INSERT INTO issue_labels" in query["query"]
    )

    assert "SELECT $1, $2, labels.id, labels.exclusivity_key" in attach
    assert "labels.workspace_id = $1" in attach


async def test_an_unknown_label_is_a_field_error_and_not_a_silent_no_op():
    """Migration 021 turned an unknown label from a foreign-key violation into
    an INSERT of zero rows, because the statement now SELECTs the label. The
    service has to notice that, or attaching a nonexistent label would report
    success."""
    connection = SequencedValueConnection([0, None])
    pool = FakePool(connection)

    with pytest.raises(ValidationError) as raised:
        await label_service(pool).attach(
            scope=TEST_SCOPE, issue_id=ISSUE_ID, label_id=LABEL_ID
        )

    assert codes(raised.value) == ["NOT_FOUND"]


# --- triage: validation and statement shape -----------------------------


@pytest.mark.parametrize("first", [0, -1, FIRST_MAX + 1])
async def test_an_out_of_range_queue_page_never_reaches_the_pool(
    exploding_pool: ExplodingPool, first
):
    """`first` is never silently clamped, matching every other list in this
    codebase: a page size outside the range is the client's to correct, and
    truncating it would serve a page nobody asked for."""
    with pytest.raises(ValidationError) as raised:
        await triage_service(exploding_pool).queue(
            scope=TEST_SCOPE, team_id=TEST_TEAM_ID, first=first, after=None
        )

    assert codes(raised.value) == ["OUT_OF_RANGE"]
    assert exploding_pool.acquire_count == 0


async def test_an_invalid_cursor_is_an_input_error_and_not_a_parser_exception(
    exploding_pool: ExplodingPool,
):
    with pytest.raises(ValidationError) as raised:
        await triage_service(exploding_pool).queue(
            scope=TEST_SCOPE, team_id=TEST_TEAM_ID, first=25, after="not-base64"
        )

    assert codes(raised.value) == ["INVALID_CURSOR"]


async def test_the_queue_reads_oldest_first_and_is_scoped_to_one_team():
    """A queue is worked from the front, unlike every other list in this
    product: the thing that has waited longest is the thing somebody has to
    look at. The team equality is what makes it a TEAM's queue rather than a
    workspace-wide pile nobody owns."""
    connection = FakeConnection(rows=[])
    pool = FakePool(connection)

    await triage_service(pool).queue(
        scope=TEST_SCOPE, team_id=TEST_TEAM_ID, first=25, after=None
    )

    sql = normalize(connection.queries[0]["query"])

    assert "issues.workspace_id = $1" in sql
    assert "issues.team_id = $2" in sql
    assert "ORDER BY issues.triage_entered_at, issues.id" in sql


async def test_the_queue_read_names_the_partial_indexs_predicate():
    """`issues_workspace_team_triage_idx` is partial on
    `triage_entered_at IS NOT NULL`, and the planner has to SEE that predicate
    to use the index. On the first page there is no keyset to imply it, so it
    is the only thing standing between this read and a scan of every issue the
    workspace has ever had."""
    connection = FakeConnection(rows=[])
    pool = FakePool(connection)

    await triage_service(pool).queue(
        scope=TEST_SCOPE, team_id=TEST_TEAM_ID, first=25, after=None
    )

    assert "triage_entered_at IS NOT NULL" in normalize(connection.queries[0]["query"])


async def test_the_queue_asks_for_one_row_more_than_the_page():
    """The extra row is what answers `hasNextPage` without a second count."""
    connection = FakeConnection(rows=[])
    pool = FakePool(connection)

    await triage_service(pool).queue(
        scope=TEST_SCOPE, team_id=TEST_TEAM_ID, first=10, after=None
    )

    assert connection.queries[0]["args"][-1] == 11


async def test_entering_triage_refuses_an_issue_already_in_a_queue():
    """`triage_entered_at IS NULL` in the predicate keeps the timestamp meaning
    "when this arrived" rather than "when somebody last pressed the button".

    The message is deliberately the same for an issue already queued, an issue
    in another workspace, an archived one and an id that exists nowhere.
    """
    connection = FakeConnection(row=None)
    pool = FakePool(connection)

    with pytest.raises(ValidationError) as raised:
        await triage_service(pool).enter(scope=TEST_SCOPE, issue_id=ISSUE_ID)

    assert codes(raised.value) == ["NOT_FOUND"]
    assert "already in triage" in raised.value.issues[0].message


async def test_accepting_an_issue_that_is_not_queued_is_refused():
    connection = FakeConnection(row=None)
    pool = FakePool(connection)

    with pytest.raises(ValidationError) as raised:
        await triage_service(pool).accept(
            scope=TEST_SCOPE, issue_id=ISSUE_ID, workflow_state_id=STATE_ID
        )

    assert codes(raised.value) == ["NOT_FOUND"]


async def test_an_issue_cannot_be_marked_a_duplicate_of_itself(
    exploding_pool: ExplodingPool,
):
    """A comparison of two arguments: it reads no row, so there is nothing to
    race, and it is settled before a connection is taken.
    `issue_relations_not_self` says the same thing in the schema and remains
    the guarantee; this only produces the better message."""
    with pytest.raises(ValidationError) as raised:
        await triage_service(exploding_pool).mark_duplicate(
            scope=TEST_SCOPE, issue_id=ISSUE_ID, duplicate_of_id=ISSUE_ID
        )

    assert codes(raised.value) == ["SELF_DUPLICATE"]
    assert exploding_pool.acquire_count == 0


async def test_declining_resolves_the_canceled_state_from_the_issues_own_team():
    """There is no `workflowStateId` on a decline, and no team id either.

    Declining is the decision that this work will not be done; the state it
    lands in is a consequence rather than a second choice, and a caller able to
    name it could decline an issue into `In Progress`. The state is resolved by
    a subquery against the issue's own stored team, so nothing has to be read
    first and no team id arrives from a client.
    """
    connection = FakeConnection(row=None)
    pool = FakePool(connection)

    with pytest.raises(ValidationError):
        await triage_service(pool).decline(scope=TEST_SCOPE, issue_id=ISSUE_ID)

    sql = normalize(connection.queries[0]["query"])

    assert "workflow_states.team_id = issues.team_id" in sql
    assert "workflow_states.type = $3" in sql
    assert connection.queries[0]["args"][2] == "canceled"


async def test_every_triage_write_requires_the_issue_to_still_be_queued():
    """The predicate that makes these operations about the QUEUE rather than a
    second way to set a workflow state -- and, for `change_team`, the one thing
    that makes renumbering an issue defensible at all."""
    for name in ("accept", "decline", "change_team"):
        source = inspect.getsource(getattr(TriageRepository, name))

        assert "triage_entered_at IS NOT NULL" in source, name


async def test_a_scope_is_required_at_every_triage_repository_entry_point():
    """A repository method that could be called without naming a tenant is one
    that can be called without one. Asserted over the signature rather than the
    body, because a default would make the omission invisible at the call
    site."""
    for name, method in inspect.getmembers(
        TriageRepository, predicate=inspect.isfunction
    ):
        if name.startswith("_"):
            continue

        parameter = inspect.signature(method).parameters.get("scope")

        assert parameter is not None, name
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is inspect.Parameter.empty, name


# --- the domain types stay pure ----------------------------------------


def test_the_domain_entities_carry_no_workspace_id():
    """A tenant on an entity is a second copy of a fact the caller holds.

    Two copies are two things that can disagree, and the one that would be
    believed is the one on the object rather than the one in the scope that
    produced it.
    """
    group = LabelGroupEntity(
        id=GROUP_ID,
        name="Status",
        exclusive=True,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )
    entry = TriageIssueEntity(issue=make_entity(1), entered_at=CREATED_AT)

    assert not hasattr(group, "workspace_id")
    assert not hasattr(entry, "workspace_id")


def test_a_label_entity_carries_its_group_id_and_not_its_groups_flag():
    """`labels.group_exclusive` is storage machinery -- it exists so PostgreSQL
    can compute a generated column from one row -- and a second place for the
    product to read exclusivity from is a second place it can be read from the
    wrong one. Exclusivity is on the GROUP."""
    entity = LabelEntity(**label_row(group_id=GROUP_ID))

    assert entity.group_id == GROUP_ID
    assert not hasattr(entity, "group_exclusive")


def test_the_triage_page_is_its_own_type():
    """Separate from `IssuePage` rather than generic over its node type: the
    two carry different entities and feed different connections, and a shared
    generic would buy one saved dataclass at the cost of a type parameter in
    every signature that mentions either."""
    page = TriageIssuePage(nodes=[], has_next_page=False, end_cursor=None)

    assert page.nodes == []


# --- the schema actually publishes all of it ---------------------------


@pytest.mark.parametrize(
    "field",
    ["triageIssues", "triageCount", "labelGroups", "labelGroup"],
)
def test_the_new_query_fields_reach_the_root(field):
    """`app.graphql.schema` assembles the root from one class per feature, and
    a class left out of the tuple vanishes from the API with the whole suite
    still green. This is the check that comment asks for."""
    assert field in build_schema("test").as_str()


@pytest.mark.parametrize(
    "field",
    [
        "triageEnter",
        "triageAccept",
        "triageDecline",
        "triageMarkDuplicate",
        "triageChangeTeam",
        "labelGroupCreate",
        "labelGroupUpdate",
        "labelGroupDelete",
        "labelSetGroup",
        "issueBulkUpdate",
        "issueBulkArchive",
    ],
)
def test_the_new_mutations_reach_the_root(field):
    assert field in build_schema("test").as_str()


def test_every_new_mutation_names_its_workspace():
    """CLAUDE.md forbids trusting a workspace id from the frontend, so every
    workspace-scoped mutation names its tenant as a SLUG and
    `app.graphql.scope` decides whether the session may act there. A mutation
    that forgot would be one an authorized-anywhere caller could aim anywhere.
    """
    sdl = build_schema("test").as_str()

    for input_type in (
        "TriageEnterInput",
        "TriageAcceptInput",
        "TriageDeclineInput",
        "TriageMarkDuplicateInput",
        "TriageChangeTeamInput",
        "LabelGroupCreateInput",
        "LabelGroupUpdateInput",
        "LabelGroupDeleteInput",
        "LabelSetGroupInput",
        "IssueBulkUpdateInput",
        "IssueBulkArchiveInput",
    ):
        block = sdl.split(f"input {input_type} {{", 1)

        assert len(block) == 2, input_type
        assert "workspaceSlug: String!" in block[1].split("}", 1)[0], input_type


def test_a_bulk_payload_carries_a_summary_and_never_a_full_issue():
    """A bulk payload holds up to BULK_MAX issues, so every field on its node
    type is charged a hundred times by the complexity rule. `Issue` selects its
    labels, its project, its cycle and its relations; a client asking for those
    over a hundred rows would buy a hundred fan-outs from a mutation that has
    already done its work."""
    sdl = build_schema("test").as_str()
    block = sdl.split("type IssueBulkPayload {", 1)[1].split("}", 1)[0]

    assert "issues: [IssueSummary!]!" in block
    assert "count: Int!" in block
