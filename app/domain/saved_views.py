"""Saved views and favourites: the entities, and the filter codec.

The interesting part of this module is the codec at the bottom. Everything
else is the shape migrations/019_saved_views.sql stores.

A saved view persists the filter `issues(filter:)` already takes -- the same
`IssueFilter`, built by the same code, turned into the same predicates -- so
that saving a list and reopening it are one filter language rather than two
that have to keep agreeing. Persisting it means turning it into JSON and back,
and the "back" half is the one with a security property attached: those bytes
were authored by whoever created the view, so they are client input that has
been sitting in a table long enough to look like server state. `decode_filter`
therefore parses rather than trusts -- known keys only, one parser per field,
anything else refused -- and what comes out is an `IssueFilter` of typed
values, indistinguishable from one that arrived live on the wire.

Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from app.domain.issues import UNSET, IssueFilter, IssueOrder, Unset
from app.domain.teams import WorkflowStateCategory


# The vocabularies migrations/019_saved_views.sql constrains, restated here so
# that this process knows them without asking the database -- and so that a
# value can be refused with a message naming the legal ones, rather than as a
# CheckViolationError carrying a rendered constraint.
#
# Two copies exist on purpose: the database has to reject an unknown layout
# whether or not the write came through this code. tests/test_saved_views.py
# pins each tuple to its CHECK rather than leaving them to agree by habit.
SAVED_VIEW_LAYOUTS: Final = ("list", "board")

SAVED_VIEW_GROUPINGS: Final = (
    "workflow_state",
    "assignee",
    "priority",
    "project",
    "cycle",
    "team",
)

SAVED_VIEW_VISIBILITIES: Final = ("personal", "shared")

# The visibility that means "the creator, and nobody else". Named rather than
# spelled inline at each comparison, because it is half of the read predicate
# for every saved-view query and a typo in one of them is a leak.
PERSONAL_VISIBILITY: Final = "personal"


class InvalidStoredFilterError(Exception):
    """A stored filter document was not one this application wrote.

    Deliberately NOT a ValidationError. Every filter reaching the table goes
    through `encode_filter`, so a document that fails to decode is not bad
    input a client can correct -- it is a row written by something that did
    not use this codec, which is a defect. Reporting it as a field error
    would tell a user to fix a request that was never the problem, and
    silently widening the filter instead would be worse: a broken narrow
    filter would render as "every issue in the workspace".

    Carries no detail about which key failed. Whoever catches this holds the
    row and can log it inside the trust boundary; the document is user text
    and does not belong in a message that travels outward.
    """

    def __init__(self):
        super().__init__("Stored filter is not readable")


@dataclass(frozen=True, slots=True)
class SavedViewEntity:
    """One named issue query, as the workspace stores it.

    `issue_filter` and `order` are the domain types the live `issues` field
    uses, already decoded. Nothing above the repository ever sees the JSON.

    `visibility` and `created_by` are carried together because neither
    answers the access question alone: a view is readable when it is shared
    OR when the viewer is its creator, and a reader holding only the first
    would show every personal view in the workspace.
    """

    id: UUID
    team_id: UUID | None
    name: str

    issue_filter: IssueFilter
    order: IssueOrder

    layout: str
    grouping: str | None
    subgrouping: str | None

    visibility: str
    created_by: UUID

    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FavoriteEntity:
    """One person's shortcut to one thing, in one workspace.

    Exactly one of the three ids is set -- `favorites_one_target` refuses
    every other row -- so a reader may take a non-None `project_id` as
    meaning the other two are None without checking.

    The user is not carried. A favourite is only ever read as part of one
    person's own list, so the id would be a copy of the argument the caller
    passed to fetch it, and a copy is a thing that can be read instead of the
    argument on the day the two stop matching.
    """

    id: UUID
    team_id: UUID | None
    project_id: UUID | None
    saved_view_id: UUID | None
    position: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SavedViewPage:
    """A forward keyset page of saved views, ordered by name.

    The same three fields as IssuePage and for the same reasons: the nodes, a
    flag the caller cannot compute for itself, and the position to resume
    from.
    """

    nodes: list[SavedViewEntity]
    has_next_page: bool
    end_cursor: str | None


# ---------------------------------------------------------------- the codec


def _decode_uuid(raw: object) -> UUID:
    if not isinstance(raw, str):
        raise InvalidStoredFilterError()

    try:
        return UUID(raw)
    except ValueError:
        raise InvalidStoredFilterError() from None


def _decode_category(raw: object) -> WorkflowStateCategory:
    if not isinstance(raw, str):
        raise InvalidStoredFilterError()

    try:
        return WorkflowStateCategory(raw)
    except ValueError:
        raise InvalidStoredFilterError() from None


def _decode_priority(raw: object) -> int:
    """An integer, and specifically not a bool.

    `isinstance(True, int)` is True in Python, so a plain int check would let
    `{"priority": true}` through as `priority = 1` -- a filter nobody wrote,
    selecting the Urgent issues. JSON has a boolean type and this field is
    not it.

    The RANGE is not checked here. `issues_priority_range` in 006 bounds the
    column, and `IssueService.list` refuses an out-of-range filter with the
    same code and message the create and update paths publish for that field
    -- so a stored 9 is answered by the one validator that already owns that
    rule, rather than by a second copy of it here that could drift.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidStoredFilterError()

    return raw


# Every field of an IssueFilter, as the key it is stored under.
#
# The attribute name IS the JSON key: one spelling rather than a mapping
# between two, so there is no table for a rename to fall out of. Used by
# `encode_filter` to walk the entity and by `decode_filter` to refuse a
# document carrying anything else.
#
# `IssueFilter` gaining a ninth field and this tuple not gaining a ninth entry
# is a field that silently stops being saved, which is why
# tests/test_saved_views.py pins the two together rather than trusting whoever
# adds it to read this comment.
_FILTER_FIELDS: Final = (
    "team_id",
    "assignee_id",
    "workflow_state_id",
    "state_category",
    "label_id",
    "priority",
    "project_id",
    "cycle_id",
)


def encode_filter(issue_filter: IssueFilter) -> dict[str, object]:
    """An IssueFilter as the object migration 019 stores.

    A field the author did not filter on is ABSENT, not null. That is the
    whole tri-state, carried through JSON's own distinction between a missing
    key and a null one: `{"assignee_id": null}` is the unassigned issues and
    `{}` is not filtering on assignee at all. Writing UNSET as null instead
    would turn every unset field into a request for the rows holding nothing,
    which is a filter that matches almost no issue.
    """
    payload: dict[str, object] = {}

    for name in _FILTER_FIELDS:
        value = getattr(issue_filter, name)

        if isinstance(value, Unset):
            continue

        payload[name] = _encode_value(value)

    return payload


def _encode_value(value: object) -> object:
    """One filter value in the spelling the decoders below read back.

    `None` survives as JSON null -- it is the "has none" filter and not a
    missing value. A UUID and an enum both become their canonical string; an
    int is already JSON.
    """
    if value is None:
        return None

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, WorkflowStateCategory):
        return value.value

    return value


def decode_filter(payload: object) -> IssueFilter:
    """A stored document back into an IssueFilter, or refuse it.

    The parse is closed, not permissive. An unknown key is refused rather
    than ignored, which is the difference that matters: ignoring it would
    make a filter that says something this server does not understand read as
    a filter that says LESS -- and a filter that says less is a wider list.
    The user who saved "my urgent issues" would get the workspace's.

    The type of each value comes from the FIELD, never from the shape of what
    is stored, so a document cannot smuggle a string into the priority
    comparison and make the driver raise from inside a query.

    Everything that comes out is a typed value the repository binds as a
    parameter, ANDed onto the workspace the caller was authorized for. So
    even a document that decodes perfectly cannot widen a list past its
    tenant: an id from another workspace narrows to nothing, exactly as it
    does when it arrives live on the wire.
    """
    if not isinstance(payload, dict):
        raise InvalidStoredFilterError()

    if not payload.keys() <= set(_FILTER_FIELDS):
        raise InvalidStoredFilterError()

    # Written out field by field rather than assembled into a dict and
    # splatted. `IssueFilter(**fields)` is three lines shorter and gives a type
    # checker nothing to check -- every field arrives as `object`, so a parser
    # wired to the wrong column would be caught by neither mypy nor a test that
    # only round-trips valid documents. Naming each field also puts the choice
    # between `_present` and `_nullable` at the field it applies to, which is
    # the one decision in this function that is easy to get wrong.
    return IssueFilter(
        team_id=_present(payload, "team_id", _decode_uuid),
        assignee_id=_nullable(payload, "assignee_id", _decode_uuid),
        workflow_state_id=_present(payload, "workflow_state_id", _decode_uuid),
        state_category=_present(payload, "state_category", _decode_category),
        label_id=_present(payload, "label_id", _decode_uuid),
        priority=_present(payload, "priority", _decode_priority),
        project_id=_nullable(payload, "project_id", _decode_uuid),
        cycle_id=_nullable(payload, "cycle_id", _decode_uuid),
    )


def _present[T](
    payload: dict,
    key: str,
    parse: Callable[[object], T],
) -> T | Unset:
    """One filter whose column is NOT NULL, where a stored null is invalid.

    A team, a workflow state, a category and a priority are set on every row,
    so "has none" is not a set anyone can ask for -- and a document saying so
    is one this codec did not write. The mirror of `_present` in
    app/graphql/inputs/issue.py, which collapses the same null for the same
    reason on the way in.
    """
    if key not in payload:
        return UNSET

    raw = payload[key]

    if raw is None:
        raise InvalidStoredFilterError()

    return parse(raw)


def _nullable[T](
    payload: dict,
    key: str,
    parse: Callable[[object], T],
) -> T | None | Unset:
    """One filter whose column is nullable, where a stored null is a filter.

    `{"assignee_id": null}` is the unassigned issues, `{"project_id": null}`
    is the ones in no project, `{"cycle_id": null}` is the backlog. Absent is
    what means "not filtering on this". The mirror of `_nullable` in
    app/graphql/inputs/issue.py.
    """
    if key not in payload:
        return UNSET

    raw = payload[key]

    if raw is None:
        return None

    return parse(raw)
