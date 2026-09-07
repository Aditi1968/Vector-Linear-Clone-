from datetime import datetime
from enum import Enum
from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.domain.issues import (
    IssueFilter,
    IssueOrder,
    IssueOrderField,
    OrderDirection,
    Unset,
)
from app.domain.saved_views import (
    SAVED_VIEW_GROUPINGS,
    SAVED_VIEW_LAYOUTS,
    SAVED_VIEW_VISIBILITIES,
    FavoriteEntity,
    SavedViewEntity,
    SavedViewPage,
)
from app.domain.teams import WorkflowStateCategory
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.graphql.errors import bad_user_input
from app.graphql.types.errors import ValidationErrorType

# Imported for their side effect as well as their names. `types/issue.py` is
# where `strawberry.enum` annotates IssueOrderField and OrderDirection with
# their GraphQL definitions, and `types/team.py` does the same for
# WorkflowStateCategory. A field referencing one of those bare classes before
# the annotating module has run makes Strawberry mint a SECOND definition for
# the same name -- at which point the schema refuses to build. Whether that
# module happens to be imported first is a question about import order, which
# is not a thing to leave to luck.
from app.graphql.types.issue import (  # noqa: F401
    IssueConnection,
    IssueOrderFieldType,
    OrderDirectionType,
)
from app.graphql.types.pagination import PageInfo
from app.graphql.types.team import WorkflowStateCategoryType  # noqa: F401


DEFAULT_VIEW_ISSUE_FIRST = 50


@strawberry.enum(
    name="SavedViewLayout",
    description="How a saved view arranges the issues it selects.",
)
class SavedViewLayoutType(Enum):
    """The members' VALUES are the strings the database stores and the
    members' NAMES are what appears in SDL, so the wire contract is `LIST`
    while the column holds `list`. Keeping both spellings in one place is what
    stops the mapping being an `if` ladder somewhere.

    Not generated from `SAVED_VIEW_LAYOUTS`: a dynamically built enum has no
    names for a type checker or an editor to know about, and this is a
    contract that should be greppable. The import-time check below asserts the
    two agree, which is what a generated enum would have made unnecessary and
    a hand-written one makes cheap.
    """

    LIST = "list"
    BOARD = "board"


@strawberry.enum(
    name="SavedViewGrouping",
    description=(
        "What a saved view gathers its rows by. The server stores and "
        "validates this choice; the grouping itself is a rendering of a page "
        "the client already holds. There is deliberately no LABEL: an issue "
        "wears many labels, so grouping by one would put the same issue in "
        "several groups and every count drawn from them would overstate the "
        "list."
    ),
)
class SavedViewGroupingType(Enum):
    WORKFLOW_STATE = "workflow_state"
    ASSIGNEE = "assignee"
    PRIORITY = "priority"
    PROJECT = "project"
    CYCLE = "cycle"
    TEAM = "team"


@strawberry.enum(
    name="SavedViewVisibility",
    description=(
        "Who can see a saved view. PERSONAL is its creator alone; SHARED is "
        "every member of the workspace. Sharing is an update of this field "
        "rather than an operation of its own."
    ),
)
class SavedViewVisibilityType(Enum):
    PERSONAL = "personal"
    SHARED = "shared"


# Checked at import time rather than left to a test. Each enum above and its
# domain tuple are two spellings of one CHECK constraint in migration 019, and
# a disagreement between them is not a failing feature -- it is a payload that
# cannot be built, raised from whichever resolver happens to read the drifted
# row first, in production, as a masked internal error. Failing at import turns
# that into a process that will not start.
#
# An `if`/`raise` and not an `assert`, because `python -O` discards asserts and
# this is a startup gate rather than a debugging aid.
def _pin(enum: type[Enum], vocabulary: tuple[str, ...], constraint: str) -> None:
    if tuple(member.value for member in enum) != vocabulary:
        raise RuntimeError(
            f"{enum.__name__} and its app.domain.saved_views tuple disagree; "
            f"they are both statements of {constraint} in "
            "migrations/019_saved_views.sql and have to be changed together"
        )


_pin(SavedViewLayoutType, SAVED_VIEW_LAYOUTS, "saved_views_layout_check")
_pin(SavedViewGroupingType, SAVED_VIEW_GROUPINGS, "saved_views_grouping_check")
_pin(SavedViewVisibilityType, SAVED_VIEW_VISIBILITIES, "saved_views_visibility_check")


@strawberry.type(
    name="SavedViewIdFilter",
    description=(
        "A filter on a column that may hold nothing. The wrapper's presence "
        "is the filter and its `id` is what to match: `{id: null}` selects "
        "the rows holding nothing -- unassigned, in no project, in no cycle "
        "-- while the wrapper itself being null means the view does not "
        "filter on that column at all."
    ),
)
class SavedViewIdFilterType:
    """The output half of the tri-state `IssueFilterInput` expresses with an
    omitted field.

    A GraphQL input can distinguish "absent" from "null"; an output cannot --
    every selected field is returned, and a returned null is one value. So the
    three filters whose column is nullable are wrapped, and the other five,
    whose columns are NOT NULL and for which "has none" is not a set anyone
    can ask for, are plain nullable scalars.
    """

    id: UUID | None


@strawberry.type(
    name="SavedViewFilter",
    description=(
        "The filter a saved view stores, in the same vocabulary "
        "`IssueFilterInput` takes. Every field is a narrowing and none of "
        "them can widen a list beyond the workspace the request was "
        "authorized for."
    ),
)
class SavedViewFilterType:
    team_id: UUID | None
    workflow_state_id: UUID | None
    state_category: WorkflowStateCategory | None
    label_id: UUID | None
    priority: int | None

    assignee: SavedViewIdFilterType | None
    project: SavedViewIdFilterType | None
    cycle: SavedViewIdFilterType | None

    @classmethod
    def from_domain(cls, issue_filter: IssueFilter) -> "SavedViewFilterType":
        return cls(
            team_id=_plain(issue_filter.team_id),
            workflow_state_id=_plain(issue_filter.workflow_state_id),
            state_category=_plain(issue_filter.state_category),
            label_id=_plain(issue_filter.label_id),
            priority=_plain(issue_filter.priority),
            assignee=_wrapped(issue_filter.assignee_id),
            project=_wrapped(issue_filter.project_id),
            cycle=_wrapped(issue_filter.cycle_id),
        )


def _plain[T](value: T | Unset) -> T | None:
    """UNSET as null, for the five filters whose column is NOT NULL.

    No information is lost: those columns hold a value on every row, so
    "filtering for nothing" is not a request anyone can make and null has only
    one meaning.
    """
    if isinstance(value, Unset):
        return None

    return value


def _wrapped(value: UUID | None | Unset) -> SavedViewIdFilterType | None:
    """UNSET as no wrapper, None as a wrapper holding null.

    The distinction this exists for: `{id: null}` is "unassigned" and a null
    wrapper is "not filtering on assignee", and collapsing them would make a
    saved view of the unassigned issues indistinguishable from one that never
    mentioned assignees.
    """
    if isinstance(value, Unset):
        return None

    return SavedViewIdFilterType(id=value)


@strawberry.type(name="SavedView")
class SavedViewType:
    id: UUID
    name: str

    # The team this view is filed under, or null for a workspace-wide one.
    #
    # A LABEL and not a filter: a view narrowed to one team carries that
    # narrowing in `filter.teamId` like every other predicate. This is which
    # sidebar the view appears in.
    #
    # An id and not a `Team`, for the reason `Project.teamIds` gives: when a
    # Team type is reachable from other people's data, `team: Team` is added
    # beside this field and this one is deprecated -- which is a change
    # clients can see coming, unlike a field that has to change type to become
    # correct.
    team_id: UUID | None

    filter: SavedViewFilterType
    order_field: IssueOrderField
    order_direction: OrderDirection

    layout: SavedViewLayoutType
    grouping: SavedViewGroupingType | None
    subgrouping: SavedViewGroupingType | None

    visibility: SavedViewVisibilityType

    # Who wrote this view, as an id. Also who may change it: every write path
    # is scoped to the creator, so a client can render an edit control by
    # comparing this with `me.id` rather than by trying the mutation.
    created_by: UUID

    created_at: datetime
    updated_at: datetime

    # Carried, not exposed: `strawberry.Private` keeps these out of the schema.
    #
    # The scope the root field AUTHORIZED, and the filter and order as domain
    # values, handed down so `issues` below runs in the same workspace its
    # parent was read from and against the same filter that was decoded from
    # the row. None of it is taken from the wire -- an `issues` field that
    # accepted a filter argument would be a way to run an arbitrary query
    # while claiming to be loading a saved one.
    #
    # Per object rather than per request, because one document may name two
    # workspaces in two root fields; a single ambient scope would serve the
    # second field's children out of the first field's tenant.
    scope: strawberry.Private[AuthorizedWorkspaceScope]
    issue_filter: strawberry.Private[IssueFilter]

    @strawberry.field(
        description=(
            "The issues this view selects, in the order it stores. Loading a "
            "saved view is this field: the filter and the ordering come from "
            "the stored row, never from the document, so the page is the one "
            "that was saved."
        )
    )
    async def issues(
        self,
        info: Info,
        first: int = DEFAULT_VIEW_ISSUE_FIRST,
        after: str | None = None,
    ) -> IssueConnection:
        """The round trip, and the only reason the codec exists.

        The stored filter reaches `IssueService.list` as an `IssueFilter` of
        typed values, which is the same thing `issues(filter:)` hands it. So
        the predicates, the tenant scoping and the validation are one code
        path whether the filter arrived on the wire a moment ago or was saved
        last year -- and there is no second filter language for a tenancy
        predicate to be missing from.

        `first` and `after` ARE arguments, because paging through a saved
        view's results is the client's business and not the view's. The cursor
        carries the ordering it was minted under and the service refuses one
        minted under a different one, so changing a view's sort while a client
        holds a cursor is refused rather than silently answered with the wrong
        rows.
        """
        try:
            page = await info.context.issue_service.list(
                scope=self.scope,
                issue_filter=self.issue_filter,
                order=IssueOrder(
                    field=self.order_field,
                    direction=self.order_direction,
                ),
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected input errors are translated. A stored filter that
            # no longer validates arrives here too -- a priority the schema
            # has since narrowed, say -- and reporting it as BAD_USER_INPUT is
            # right: the fix is to edit the view, which is something the
            # caller can do.
            raise bad_user_input("Invalid issue list arguments", exc) from None

        return IssueConnection.from_domain(page, self.scope, self.issue_filter)

    @classmethod
    def from_entity(
        cls,
        entity: SavedViewEntity,
        scope: AuthorizedWorkspaceScope,
    ) -> "SavedViewType":
        return cls(
            scope=scope,
            issue_filter=entity.issue_filter,
            id=entity.id,
            name=entity.name,
            team_id=entity.team_id,
            filter=SavedViewFilterType.from_domain(entity.issue_filter),
            order_field=entity.order.field,
            order_direction=entity.order.direction,
            # By value: the entity carries what the column holds, and the enums
            # are keyed on exactly those strings. A row holding a value an enum
            # does not know raises ValueError here rather than being rendered
            # as something plausible -- which is the right failure, since the
            # only way to store one is a migration that widened a CHECK without
            # widening this.
            layout=SavedViewLayoutType(entity.layout),
            grouping=(
                None
                if entity.grouping is None
                else SavedViewGroupingType(entity.grouping)
            ),
            subgrouping=(
                None
                if entity.subgrouping is None
                else SavedViewGroupingType(entity.subgrouping)
            ),
            visibility=SavedViewVisibilityType(entity.visibility),
            created_by=entity.created_by,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class SavedViewConnection:
    nodes: list[SavedViewType]
    page_info: PageInfo

    @classmethod
    def from_domain(
        cls,
        page: SavedViewPage,
        scope: AuthorizedWorkspaceScope,
    ) -> "SavedViewConnection":
        return cls(
            nodes=[SavedViewType.from_entity(entity, scope) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


@strawberry.type(
    name="Favorite",
    description=(
        "One shortcut in one person's sidebar. Exactly one of `teamId`, "
        "`projectId` and `savedViewId` is set."
    ),
)
class FavoriteType:
    id: UUID

    # Three nullable ids rather than a union, and the reason is the same one
    # `Project.teamIds` gives: this schema has no Team type, and a union
    # member that does not exist cannot be declared. A union also forces every
    # client to write an inline fragment per member before it can read an id,
    # which is a lot of document for a sidebar row. When the three object
    # types exist, a `target` union is added beside these and they are
    # deprecated.
    team_id: UUID | None
    project_id: UUID | None
    saved_view_id: UUID | None

    position: int = strawberry.field(
        description=(
            "Where this sits in the person's own list. Neither unique nor "
            "contiguous: ties are broken by id, so two favorites sharing a "
            "number are ordered stably rather than ambiguously."
        )
    )

    created_at: datetime

    @classmethod
    def from_entity(cls, entity: FavoriteEntity) -> "FavoriteType":
        return cls(
            id=entity.id,
            team_id=entity.team_id,
            project_id=entity.project_id,
            saved_view_id=entity.saved_view_id,
            position=entity.position,
            created_at=entity.created_at,
        )


# One payload type per SHAPE, reused across the mutations that share it, rather
# than one per mutation. The convention of a distinct payload per mutation
# exists so a field can be added for one operation without appearing on the
# others; that is real and it is not free, and four types differing only in
# name are four places to add the next common field and four chances to add it
# to three of them. The first time one of these needs a field the others do
# not, it gets its own type.
@strawberry.type
class SavedViewPayload:
    saved_view: SavedViewType | None
    errors: list[ValidationErrorType]


@strawberry.type
class SavedViewDeletePayload:
    # The id rather than the view. Returning the deleted row would invite a
    # client to render something that no longer exists; the id is what a cache
    # needs in order to evict it.
    deleted_saved_view_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class FavoritePayload:
    favorite: FavoriteType | None
    errors: list[ValidationErrorType]


@strawberry.type
class FavoriteDeletePayload:
    deleted_favorite_id: UUID | None
    errors: list[ValidationErrorType]
