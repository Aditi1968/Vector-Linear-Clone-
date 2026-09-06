"""Sub-issues and relations: the rules that hold without a database.

Three subjects here, and the third is the one worth naming.

  * The relation vocabulary. Four names a client may say, three values the
    table stores, and a canonicalisation between them. That mapping is
    pure -- no connection, no scope -- and it is where the symmetric-relation
    decision actually lives, so it is asserted directly rather than inferred
    from what a page of rows came back looking like.
  * The two refusals settled before a connection is taken. Self-parenting and
    self-relating compare two arguments and read no row, so a pool that
    explodes on `acquire()` is the assertion: if either ever grows a SELECT,
    these fail.
  * The GraphQL wiring. `app.graphql.schema` composes the root types with
    `merge_types`, and a feature that adds a mutation class and forgets the
    tuple gets a schema that builds, a suite that passes, and an API missing
    four fields. That failure is invisible to every other test in this
    repository, so it is asserted against the built schema here.

The constraints themselves -- cross-tenant refusal, duplicate refusal, the
cycle guard -- are claims about a server and are in tests/test_relations_db.py.
Nothing in this file reaches a database.
"""

import pytest

from app.domain.errors import ValidationError
from app.domain.relations import (
    SYMMETRIC_TYPES,
    RelationType,
    invert,
)
from app.domain.tenancy import WorkspaceScope
from app.graphql.limits import ASSUMED_PAGE_SIZE
from app.graphql.schema import build_schema
from app.graphql.types.relations import IssueRelationTypeEnum
from app.repositories.relations import RelationRepository, _canonical
from app.services.relations import FIRST_MAX, RelationService

from tests.conftest import ExplodingPool


SCOPE = WorkspaceScope(workspace_id="00000000-0000-7000-8000-000000000001")

# Two ids whose order is unambiguous, so a test about canonical ordering can
# say which one is "lower" without depending on how a UUID happens to sort.
LOW = "a1b2c3d4-0000-4000-8000-000000000001"
HIGH = "a1b2c3d4-0000-4000-8000-000000000002"


@pytest.fixture
def service() -> RelationService:
    """A service whose pool fails the test if anything acquires a connection."""
    return RelationService(pool=ExplodingPool(), repository=RelationRepository())


# --- the vocabulary ----------------------------------------------------


def test_the_graphql_enum_and_the_domain_enum_carry_the_same_values():
    """The two enums are converted by value, so drift is a KeyError at runtime.

    `IssueRelationTypeEnum` is declared separately from `RelationType` so
    that no Strawberry attribute lands on a domain class, and its
    `to_domain`/`from_domain` are `RelationType(self.value)` lookups rather
    than a translation table. That is only safe while the value sets are
    identical, and nothing in the type system says they are.
    """
    assert {member.value for member in IssueRelationTypeEnum} == {
        member.value for member in RelationType
    }

    for member in IssueRelationTypeEnum:
        assert member.to_domain() is RelationType(member.value)
        assert IssueRelationTypeEnum.from_domain(member.to_domain()) is member


def test_inverting_twice_returns_the_original_name():
    """`invert` is total and is its own undo, for every one of the four names.

    Totality is what lets `list_relations` invert unconditionally instead of
    asking whether a type is symmetric first. If a fifth name were added
    without an entry, this raises KeyError rather than silently making one
    direction of one relation unreadable.
    """
    for relation_type in RelationType:
        assert invert(invert(relation_type)) is relation_type


def test_the_symmetric_types_are_exactly_the_ones_that_are_their_own_inverse():
    """SYMMETRIC_TYPES and the inverse map are two statements of one fact.

    `_canonical` reads the set and `list_relations` reads the map, so the
    two disagreeing would mean a relation stored in canonical order and read
    back under a name the client never sent.
    """
    assert SYMMETRIC_TYPES == {
        relation_type
        for relation_type in RelationType
        if invert(relation_type) is relation_type
    }


# --- canonicalisation --------------------------------------------------
#
# The decision migration 010 is shaped around: one row per relationship, not
# one per direction. Every claim the migration's `issue_relations_unique`
# makes rests on these four cases being right.


def test_blocked_by_is_stored_as_blocks_with_the_ends_exchanged():
    """The direction is preserved by swapping, never by storing a fifth name.

    `issue_relations_type_known` refuses 'blocked_by' outright, so a
    canonicalisation that passed it through would fail at the INSERT. The
    point of asserting here is that it must not merely fail -- it must come
    out as the same edge said the other way.
    """
    assert _canonical(LOW, HIGH, RelationType.BLOCKED_BY) == (HIGH, LOW, "blocks")


def test_blocks_is_stored_exactly_as_it_was_given():
    """Ordering a directed relation would destroy its entire content.

    `issue_relations_symmetric_ordered` exempts 'blocks' for this reason,
    and the exemption is only correct if nothing here reorders it. Both
    argument orders are asserted, because a rule that fired on one of them
    would be a rule that quietly reverses half the blocking graph.
    """
    assert _canonical(LOW, HIGH, RelationType.BLOCKS) == (LOW, HIGH, "blocks")
    assert _canonical(HIGH, LOW, RelationType.BLOCKS) == (HIGH, LOW, "blocks")


@pytest.mark.parametrize("relation_type", sorted(SYMMETRIC_TYPES))
def test_a_symmetric_relation_reaches_one_row_whichever_way_it_is_said(relation_type):
    """Lower id first, both ways round -- which is what makes UNIQUE mean it.

    This is the whole symmetric-duplicate argument in one assertion. Said
    either way, the pair reduces to one tuple, so `issue_relations_unique`
    refuses the second insert rather than accepting a synonym of the first.
    A constraint over (source, target, type) would happily hold both orders,
    because they are different tuples -- they are simply not different
    relationships.
    """
    assert (
        _canonical(LOW, HIGH, relation_type)
        == _canonical(HIGH, LOW, relation_type)
        == (LOW, HIGH, relation_type.value)
    )


def test_no_canonical_form_ever_carries_a_name_the_table_refuses():
    """Every one of the four inputs reduces to one of the three stored values.

    Asserted against the CHECK's vocabulary written out here rather than
    imported, so that widening the constraint in a future migration does not
    also widen the expectation without anyone saying so.
    """
    for relation_type in RelationType:
        _, _, stored = _canonical(LOW, HIGH, relation_type)

        assert stored in {"blocks", "related", "duplicate"}


# --- refusals settled before a connection ------------------------------


async def test_an_issue_cannot_be_its_own_parent(service):
    """Refused by comparing two arguments, with no row read and none needed.

    `issues_parent_not_self` says the same thing in the database and remains
    the guarantee. This asserts the better message arrives without a round
    trip -- the exploding pool is the assertion.
    """
    with pytest.raises(ValidationError) as raised:
        await service.set_parent(scope=SCOPE, issue_id=LOW, parent_id=LOW)

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("parentId", "SELF_PARENT")
    ]


async def test_an_issue_cannot_be_related_to_itself(service):
    """The same shape as self-parenting, and refused the same way."""
    with pytest.raises(ValidationError) as raised:
        await service.create_relation(
            scope=SCOPE,
            source_issue_id=LOW,
            target_issue_id=LOW,
            relation_type=RelationType.BLOCKS,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("targetIssueId", "SELF_RELATION")
    ]


@pytest.mark.parametrize("first", [0, -1, FIRST_MAX + 1])
async def test_a_page_size_outside_the_range_is_refused_before_a_connection(
    service, first
):
    """`first` is never silently clamped, on either connection this service serves."""
    for call in (
        service.list_children(scope=SCOPE, parent_id=LOW, first=first, after=None),
        service.list_relations(scope=SCOPE, issue_id=LOW, first=first, after=None),
    ):
        with pytest.raises(ValidationError) as raised:
            await call

        assert [(issue.field, issue.code) for issue in raised.value.issues] == [
            ("first", "OUT_OF_RANGE")
        ]


async def test_a_malformed_cursor_is_expected_input_rather_than_an_exception(service):
    with pytest.raises(ValidationError) as raised:
        await service.list_relations(
            scope=SCOPE,
            issue_id=LOW,
            first=10,
            after="not-a-cursor",
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("after", "INVALID_CURSOR")
    ]


def test_the_page_ceiling_matches_what_the_complexity_rule_charges():
    """A page nobody can be charged for is a limit under-charging, not a limit.

    `app.graphql.limits` prices a list field it cannot read at validation
    time at ASSUMED_PAGE_SIZE. If this service served more rows than that,
    a client could buy more work than the budget measured -- so the two
    numbers are pinned to each other rather than to a comment.
    """
    assert FIRST_MAX == ASSUMED_PAGE_SIZE


# --- the wiring --------------------------------------------------------


def _type_block(type_name: str) -> str:
    """The SDL body of one type, read off the schema the app actually builds.

    Read from the built schema rather than from `frontend/schema.graphql`,
    which is a generated artefact: a stale export would make these assertions
    agree with a file instead of with the API.
    """
    return build_schema("test").as_str().split(f"type {type_name} {{")[1].split("}")[0]


@pytest.mark.parametrize(
    "field",
    [
        "issueSetParent",
        "issueClearParent",
        "issueRelationCreate",
        "issueRelationDelete",
    ],
)
def test_every_relation_mutation_reaches_the_merged_root_type(field):
    """The four mutations are on the API, not merely defined in a module.

    `RelationMutation` is composed into the root by `merge_types` in
    app/graphql/schema.py. Nothing else in this suite would notice its
    absence from that tuple: the class would still import, the schema would
    still build, and every other test would still pass while four fields
    were missing from the product's API.
    """
    assert field in _type_block("Mutation"), (
        f"{field} is missing from the root Mutation; check the merge_types "
        "tuple in app/graphql/schema.py"
    )


def test_merging_the_relation_mutations_did_not_displace_the_issue_mutation():
    """The regression `merge_types` exists to make loud, asserted anyway.

    A root class assembled by inheritance rather than by merging once
    shadowed the merged root and removed fields from the API with every test
    still green. This is the cheap standing check that the tuple grew rather
    than got replaced.
    """
    mutation = _type_block("Mutation")

    assert "issueCreate" in mutation
    assert "login" in mutation


def test_an_issue_exposes_its_edges_and_none_of_them_leads_back_to_an_issue():
    """The schema is acyclic by construction, which is stronger than MAX_DEPTH.

    `Issue.parent`, `Issue.children` and `Issue.relations` all return
    `IssueSummary`-shaped types, and `IssueSummary` has no edges at all. So
    `parent { children { parent { ... } } }` is not a document that gets
    refused at depth ten -- it is not a document that parses.
    """
    issue = _type_block("Issue")
    summary = _type_block("IssueSummary")

    for name in ("parent", "children", "relations"):
        assert name in issue
        assert name not in summary
