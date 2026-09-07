"""Saved views and favorites, without a database.

The subject of most of this file is one function pair: `encode_filter` and
`decode_filter` in app/domain/saved_views.py. A saved view stores the filter
`issues(filter:)` already takes, and the whole feature rests on the claim that
what comes back out is what went in -- save a list, reopen it, get the same
issues. So the round-trip is asserted field by field rather than on one
representative filter, because the three fields whose column is nullable carry
a distinction the other five do not, and that distinction is exactly the one a
codec loses quietly.

The second subject is what the decoder refuses. A stored filter is a document
some user chose the bytes of; a decoder that ignored an unfamiliar key, or
read `true` as a priority of 1, would turn a narrow saved list into a wider
one -- which is a leak that looks like a feature working. The refusals below
are the ones that would each be a leak on their own.

The rest pins the vocabularies: three enums, three domain tuples and three
CHECK constraints in migrations/019_saved_views.sql, which have to say the
same thing and are three separate places to change.

No `db` mark: nothing here reaches PostgreSQL. tests/test_migration_019_db.py
is where the constraints themselves are exercised.
"""

import re
from dataclasses import fields
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.errors import ValidationError
from app.domain.issues import (
    UNSET,
    IssueFilter,
    IssueOrder,
    IssueOrderField,
    OrderDirection,
)
from app.domain.saved_views import (
    SAVED_VIEW_GROUPINGS,
    SAVED_VIEW_LAYOUTS,
    SAVED_VIEW_VISIBILITIES,
    InvalidStoredFilterError,
    decode_filter,
    encode_filter,
)
from app.domain.teams import WorkflowStateCategory
from app.graphql.types.saved_view import (
    SavedViewFilterType,
    SavedViewGroupingType,
    SavedViewLayoutType,
    SavedViewVisibilityType,
)
from app.repositories.saved_views import FavoriteRepository, SavedViewRepository
from app.services.saved_views import FavoriteService, SavedViewService

from tests.conftest import TEST_AUTHORIZED_SCOPE


MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations" / "019_saved_views.sql"
).read_text(encoding="utf-8")

TEAM_ID = UUID("00000000-0000-7000-8000-0000000000a1")
ASSIGNEE_ID = UUID("00000000-0000-7000-8000-0000000000a2")
STATE_ID = UUID("00000000-0000-7000-8000-0000000000a3")
LABEL_ID = UUID("00000000-0000-7000-8000-0000000000a4")
PROJECT_ID = UUID("00000000-0000-7000-8000-0000000000a5")
CYCLE_ID = UUID("00000000-0000-7000-8000-0000000000a6")

# One filter naming every field at once. Not a realistic screen -- nobody
# filters on eight things -- and that is the point: a round-trip over a
# one-field filter passes for a codec that drops seven.
EVERYTHING = IssueFilter(
    team_id=TEAM_ID,
    assignee_id=ASSIGNEE_ID,
    workflow_state_id=STATE_ID,
    state_category=WorkflowStateCategory.STARTED,
    label_id=LABEL_ID,
    priority=2,
    project_id=PROJECT_ID,
    cycle_id=CYCLE_ID,
)


# --- the round trip ---------------------------------------------------


def test_the_wide_filter_survives_as_an_empty_object():
    """A view that narrows nothing is an ordinary view -- "all issues,
    grouped by status" -- so it has to be storable, and it has to come back
    as the wide filter rather than as eight filters for nothing."""
    assert encode_filter(IssueFilter()) == {}
    assert decode_filter({}) == IssueFilter()


def test_every_field_survives_the_round_trip():
    """The claim the whole feature rests on, over a filter naming all eight.

    Compared as whole dataclasses rather than field by field, so a codec that
    round-trips seven fields and silently drops the eighth fails here rather
    than in whichever screen happened to use it.
    """
    assert decode_filter(encode_filter(EVERYTHING)) == EVERYTHING


@pytest.mark.parametrize("field", [f.name for f in fields(IssueFilter)])
def test_the_codec_knows_every_field_an_issue_filter_has(field):
    """Parametrized over `IssueFilter` itself, not over a list written here.

    A ninth filter field added to the domain and forgotten in the codec would
    be a filter a client can send to `issues` and cannot save -- silently,
    because `encode_filter` walks its own tuple and would simply not see it.
    This is what turns that into a failing test on the commit that adds the
    field.
    """
    one = IssueFilter(**{field: getattr(EVERYTHING, field)})

    assert field in encode_filter(one)
    assert decode_filter(encode_filter(one)) == one


@pytest.mark.parametrize("field", ["assignee_id", "project_id", "cycle_id"])
def test_a_filter_for_the_rows_holding_nothing_is_not_the_absence_of_one(field):
    """The distinction a codec loses quietly, and the reason this file exists.

    "Unassigned issues", "issues in no project" and "the backlog" are three
    real saved views, and each is `None` where the wide filter has UNSET. A
    codec that wrote UNSET as null -- or read null as UNSET -- would turn one
    into the other: a saved view of the unassigned issues would silently
    become a saved view of everything.
    """
    asked_for_nothing = IssueFilter(**{field: None})

    assert encode_filter(asked_for_nothing) == {field: None}
    assert decode_filter({field: None}) == asked_for_nothing

    # And the other direction: the wide filter must not mention the field.
    assert field not in encode_filter(IssueFilter())
    assert getattr(decode_filter({}), field) is UNSET


# --- what the decoder refuses -----------------------------------------


def test_an_unknown_key_is_refused_rather_than_ignored():
    """Ignoring it would make the filter say LESS, and less is wider.

    That is the whole argument for a closed parse. A document holding a key
    this server does not understand is one written by something else -- a
    newer deployment, a hand-run UPDATE, an attacker -- and reading it as
    "the other seven filters" hands back a list the view's author never saved.
    """
    payload = encode_filter(EVERYTHING) | {"archived": True}

    with pytest.raises(InvalidStoredFilterError):
        decode_filter(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"team_id": None},
        {"workflow_state_id": None},
        {"state_category": None},
        {"label_id": None},
        {"priority": None},
    ],
)
def test_a_null_under_a_not_null_column_is_refused(payload):
    """`IS NULL` on a NOT NULL column selects nothing that any state of the
    database could produce, so a document asking for it is not a filter -- it
    is a row this codec did not write. Refused rather than read as "no
    filter", which would again widen the list."""
    with pytest.raises(InvalidStoredFilterError):
        decode_filter(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"team_id": 7},
        {"team_id": "not-a-uuid"},
        {"team_id": ["00000000-0000-7000-8000-0000000000a1"]},
        {"state_category": "invented"},
        {"state_category": 3},
        {"priority": "2"},
        {"priority": 2.5},
    ],
)
def test_a_value_of_the_wrong_shape_is_refused(payload):
    """The type comes from the FIELD and never from the stored value.

    Without that, a document could put a string where the repository binds an
    integer and make asyncpg raise from inside a query -- an internal error on
    a page that was only trying to list issues.
    """
    with pytest.raises(InvalidStoredFilterError):
        decode_filter(payload)


def test_a_boolean_priority_is_refused():
    """`isinstance(True, int)` is True in Python.

    So the obvious int check lets `{"priority": true}` through as `priority =
    1`, which is a filter for the Urgent issues that nobody wrote. Asserted
    separately from the other shapes because it is the one that passes a
    plain type check.
    """
    with pytest.raises(InvalidStoredFilterError):
        decode_filter({"priority": True})


@pytest.mark.parametrize("payload", [None, [], "{}", 3, True])
def test_a_document_that_is_not_an_object_is_refused(payload):
    """The application half of `saved_views_filter_is_object`.

    The CHECK stops these reaching the column at all, and this stops the
    decoder assuming the CHECK is there -- it is the schema of one deployment,
    and this function is what runs against a row that arrived any other way.
    """
    with pytest.raises(InvalidStoredFilterError):
        decode_filter(payload)


def test_the_refusal_carries_no_part_of_the_document():
    """The message is fixed text. A stored filter is user-authored, so
    echoing any of it into an exception puts client bytes into a string that
    travels up the stack and into logs."""
    with pytest.raises(InvalidStoredFilterError) as raised:
        decode_filter({"team_id": "secret-looking-value"})

    assert "secret" not in str(raised.value)


# --- the output type keeps the same distinction -----------------------


@pytest.mark.parametrize(
    ("field", "wrapper"),
    [
        ("assignee_id", "assignee"),
        ("project_id", "project"),
        ("cycle_id", "cycle"),
    ],
)
def test_the_output_type_tells_filtering_for_null_from_not_filtering(field, wrapper):
    """A GraphQL output has no "absent", so the three nullable filters are
    wrapped.

    Without the wrapper both cases render as `assigneeId: null` and a client
    reopening the view's filter bar cannot tell "unassigned" from "no
    assignee filter" -- so it would draw the wrong chip, and an edit saved
    from that screen would silently widen the view.
    """
    not_filtering = SavedViewFilterType.from_domain(IssueFilter())
    for_nothing = SavedViewFilterType.from_domain(IssueFilter(**{field: None}))

    assert getattr(not_filtering, wrapper) is None
    assert getattr(for_nothing, wrapper) is not None
    assert getattr(for_nothing, wrapper).id is None


def test_the_output_type_carries_the_values_it_was_given():
    rendered = SavedViewFilterType.from_domain(EVERYTHING)

    assert rendered.team_id == TEAM_ID
    assert rendered.workflow_state_id == STATE_ID
    assert rendered.state_category is WorkflowStateCategory.STARTED
    assert rendered.label_id == LABEL_ID
    assert rendered.priority == 2
    assert rendered.assignee.id == ASSIGNEE_ID
    assert rendered.project.id == PROJECT_ID
    assert rendered.cycle.id == CYCLE_ID


# --- the vocabularies, pinned to the constraints ----------------------


def _check_values(constraint: str) -> tuple[str, ...]:
    """The quoted strings inside one named CHECK in migration 019.

    Read out of the file rather than restated here, so this asserts what the
    database will actually enforce rather than what a second copy in a test
    says it enforces.

    The body is delimited by balancing parentheses rather than by a regex over
    lines, because the CHECKs in that file are written both ways -- one line
    for the short vocabularies and several for the long ones -- and a
    line-shaped pattern silently matches nothing on whichever form it was not
    written for. Balancing also keeps the prose around a constraint out of the
    match, so a comment mentioning a value cannot be read as one.
    """
    named = MIGRATION.find(f"CONSTRAINT {constraint}")

    assert named != -1, f"{constraint} is not in migrations/019_saved_views.sql"

    start = MIGRATION.index("CHECK (", named) + len("CHECK ")
    depth = 0

    for index in range(start, len(MIGRATION)):
        if MIGRATION[index] == "(":
            depth += 1
        elif MIGRATION[index] == ")":
            depth -= 1

            if depth == 0:
                body = MIGRATION[start : index + 1]

                return tuple(re.findall(r"'([a-z_]+)'", body))

    raise AssertionError(f"{constraint} has an unbalanced CHECK body")


@pytest.mark.parametrize(
    ("enum", "vocabulary", "constraint"),
    [
        (SavedViewLayoutType, SAVED_VIEW_LAYOUTS, "saved_views_layout_check"),
        (SavedViewGroupingType, SAVED_VIEW_GROUPINGS, "saved_views_grouping_check"),
        (
            SavedViewVisibilityType,
            SAVED_VIEW_VISIBILITIES,
            "saved_views_visibility_check",
        ),
    ],
)
def test_each_vocabulary_is_the_same_in_all_three_places(enum, vocabulary, constraint):
    """Three copies of one list: a CHECK, a domain tuple and a GraphQL enum.

    They exist separately on purpose -- the database has to refuse an unknown
    layout whether or not the write came through this code, and this process
    has to know the vocabulary without asking the database. What must not
    happen is drift, and drift is silent: a member added to the enum and not
    to the CHECK is a value a client can send and the server cannot store,
    reported as a masked internal error.
    """
    assert tuple(member.value for member in enum) == vocabulary
    assert _check_values(constraint) == vocabulary


def test_the_stored_ordering_vocabulary_matches_the_domain_enums():
    """`order_field` and `order_direction` are TEXT columns holding
    IssueOrderField and OrderDirection values, so the same drift is possible
    one table over -- and there it would be a saved view that cannot be
    loaded, since the repository maps the column back through the enum."""
    assert _check_values("saved_views_order_field_check") == tuple(
        member.value for member in IssueOrderField
    )
    assert _check_values("saved_views_order_direction_check") == tuple(
        member.value for member in OrderDirection
    )


def test_a_saved_ordering_is_one_the_issue_list_can_actually_serve():
    """Every combination the columns admit is a real IssueOrder.

    The ordering is stored as two independent columns with two independent
    CHECKs, so nothing in the schema stops a pairing the application has no
    meaning for. This asserts there is no such pairing -- every field works
    in both directions -- which is what makes the two-column shape safe.
    """
    for field in IssueOrderField:
        for direction in OrderDirection:
            order = IssueOrder(field=field, direction=direction)

            assert order.token == f"{field.value}:{direction.value}"


# --- the service refuses before it opens a connection -----------------


def saved_view_service(pool) -> SavedViewService:
    """The service over a pool that fails if anything acquires a connection.

    Validation must reject bad input before a connection is ever taken, so any
    acquire during a validation test is a test failure by definition -- which
    is what `ExplodingPool` turns into a message instead of a silent pass.
    """
    return SavedViewService(
        pool=pool,
        repository=SavedViewRepository(),
        favorites=FavoriteRepository(),
    )


async def test_a_null_name_is_a_field_error_and_not_a_crash(exploding_pool):
    """The one input this validator is most likely to fall over on.

    GraphQL makes an input field required exactly when it is non-null with no
    default, so `name` on a PATCH has to be declared nullable -- which means
    `savedViewUpdate(name: null)` is a document the schema accepts and the
    service has to answer. `len(None)` raises TypeError, which the transport
    masks as "Internal server error": the client would be told nothing, and
    the log would show a crash rather than a rejected request.
    """
    with pytest.raises(ValidationError) as raised:
        await saved_view_service(exploding_pool).update(
            scope=TEST_AUTHORIZED_SCOPE,
            saved_view_id=TEAM_ID,
            name=None,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("name", "REQUIRED")
    ]
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"name": ""}, [("name", "REQUIRED")]),
        ({"name": "x" * 201}, [("name", "TOO_LONG")]),
        ({"layout": "kanbanish"}, [("layout", "INVALID")]),
        ({"layout": None}, [("layout", "REQUIRED")]),
        ({"visibility": "team"}, [("visibility", "INVALID")]),
        ({"grouping": "label"}, [("grouping", "INVALID")]),
        # A grouping this schema knows, subgrouped by itself: one group per
        # group, which is a second level that adds no level.
        (
            {"grouping": "assignee", "subgrouping": "assignee"},
            [("subgrouping", "INVALID")],
        ),
        # Subgrouped with the grouping explicitly cleared in the same patch.
        (
            {"grouping": None, "subgrouping": "assignee"},
            [("subgrouping", "INVALID")],
        ),
        # The filter is validated by the same function the live issue list
        # runs, which is the point of sharing it: a priority no row can hold
        # is refused when the view is SAVED rather than when it is opened.
        (
            {"issue_filter": IssueFilter(priority=9)},
            [("priority", "OUT_OF_RANGE")],
        ),
    ],
)
async def test_the_service_refuses_bad_input_before_taking_a_connection(
    exploding_pool, patch, expected
):
    with pytest.raises(ValidationError) as raised:
        await saved_view_service(exploding_pool).update(
            scope=TEST_AUTHORIZED_SCOPE,
            saved_view_id=TEAM_ID,
            **patch,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == expected
    assert exploding_pool.acquire_count == 0


async def test_a_favorite_names_exactly_one_target(exploding_pool):
    """Checked in the service and not left to `favorites_one_target`.

    A client that named two targets or none needs to be told which field to
    fix, and a CheckViolationError carrying a rendered constraint says nothing
    a UI can act on. The constraint is still there for writes that do not come
    through this code; tests/test_migration_019_db.py exercises it.
    """
    service = FavoriteService(pool=exploding_pool, repository=FavoriteRepository())

    for targets in ({}, {"team_id": TEAM_ID, "project_id": PROJECT_ID}):
        with pytest.raises(ValidationError) as raised:
            await service.add(scope=TEST_AUTHORIZED_SCOPE, **targets)

        assert [issue.field for issue in raised.value.issues] == ["target"]

    assert exploding_pool.acquire_count == 0


async def test_a_negative_favorite_position_is_refused(exploding_pool):
    """`favorites_position_check` says the same thing, and would say it as a
    masked internal error. The bound is small, public and stated here."""
    service = FavoriteService(pool=exploding_pool, repository=FavoriteRepository())

    with pytest.raises(ValidationError) as raised:
        await service.reorder(
            scope=TEST_AUTHORIZED_SCOPE,
            favorite_id=TEAM_ID,
            position=-1,
        )

    assert [(issue.field, issue.code) for issue in raised.value.issues] == [
        ("position", "OUT_OF_RANGE")
    ]
    assert exploding_pool.acquire_count == 0


@pytest.mark.parametrize(
    ("first", "after", "field"),
    [
        (0, None, "first"),
        (101, None, "first"),
        (50, "not-a-cursor", "after"),
    ],
)
async def test_the_page_arguments_follow_the_rules_the_issue_list_publishes(
    exploding_pool, first, after, field
):
    """One page-size contract across the API, not one per list endpoint.

    The bounds and the codes are IssueService's; two list fields that
    disagreed about the legal page size would be a contract a client has to
    learn twice.
    """
    with pytest.raises(ValidationError) as raised:
        await saved_view_service(exploding_pool).list(
            scope=TEST_AUTHORIZED_SCOPE,
            first=first,
            after=after,
        )

    assert [issue.field for issue in raised.value.issues] == [field]
    assert exploding_pool.acquire_count == 0
