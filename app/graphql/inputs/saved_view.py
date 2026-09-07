from uuid import UUID

import strawberry
from strawberry.types.unset import UnsetType as StrawberryUnsetType

from app.domain.patch import UNSET, UnsetType
from app.graphql.inputs.issue import IssueFilterInput, IssueOrderInput
from app.graphql.types.saved_view import (
    SavedViewGroupingType,
    SavedViewLayoutType,
    SavedViewVisibilityType,
)


def patched[T](value: T) -> T | UnsetType:
    """Translate Strawberry's absent-field sentinel into the domain's.

    The two sentinels exist for the same reason and belong to different
    layers. `strawberry.UNSET` is what an omitted input field arrives as, and
    it is a Strawberry object; services and domain code must not import
    Strawberry (CLAUDE.md), so it stops here, exactly as `Info`, `UUID`
    coercion and every other transport concern does.

    Lives beside the inputs rather than in the mutations module, because the
    queries need it too: `savedViews(teamId:)` has the same three states an
    input field has, and two copies of this would be two things that can
    drift. It is deliberately NOT shared with
    app/graphql/mutations/projects.py, which has its own -- unifying the two
    is a change to that feature's file, and this one has no business making
    it.

    `isinstance` and not `is`: identical at runtime, but an input field is
    annotated `str | None` while carrying an `UnsetType` at runtime -- a lie
    Strawberry tells on purpose -- and an identity test between two types mypy
    believes cannot overlap is reported as a mistake under `strict_equality`.
    """
    if isinstance(value, StrawberryUnsetType):
        return UNSET

    return value


@strawberry.input
class SavedViewCreateInput:
    """The fields a client may set when saving a view.

    `filter` and `orderBy` are the SAME input objects `issues(filter:,
    orderBy:)` takes, and that is the whole design rather than a convenience.
    A client saves the list it is looking at by sending the arguments it just
    sent, and the server stores what it would have executed -- so there is one
    filter language, one set of predicates, and one place a tenancy predicate
    could be missing from. A `SavedViewFilterInput` of its own would be a
    second dialect that has to keep agreeing with the first, and the copy that
    drifts is the one nobody re-read.

    Every optional field defaults to a plain value rather than to UNSET, and
    the difference from SavedViewUpdateInput below is not an inconsistency. On
    a create there is no prior value for "leave it alone" to preserve, so "not
    supplied" and "supplied as null" describe the same resulting row -- one
    sentinel would be distinguishing two things that cannot differ.

    There is deliberately no `createdBy`. Authorship is taken from whoever
    authenticated the request, because a client able to name the creator could
    forge it -- and `createdBy` is not decoration here, it is half of the read
    predicate for a personal view and the whole of the write predicate for
    every view.
    """

    # A slug and not a workspace id, deliberately. CLAUDE.md forbids trusting a
    # workspace id from the frontend: the slug is a public string that selects
    # WHAT is being asked about, and `app.graphql.scope` decides whether the
    # session behind the request may act there.
    workspace_slug: str

    name: str

    # Which sidebar this view belongs in, or null for the workspace's own.
    #
    # A label rather than a filter: a view narrowed to one team says so in
    # `filter.teamId` like every other predicate. A team from another
    # workspace is refused by `saved_views_team_fk` against the authorized
    # workspace, inside the insert, rather than by a check here.
    team_id: UUID | None = None

    filter: IssueFilterInput | None = None
    order_by: IssueOrderInput | None = None

    layout: SavedViewLayoutType = SavedViewLayoutType.LIST
    grouping: SavedViewGroupingType | None = None
    subgrouping: SavedViewGroupingType | None = None

    # Personal by default. A view is private until its author decides
    # otherwise, which is the only default that cannot surprise someone by
    # publishing a half-finished list of their own work to the workspace.
    visibility: SavedViewVisibilityType = SavedViewVisibilityType.PERSONAL


@strawberry.input
class SavedViewUpdateInput:
    """A patch: every field is optional, and omission means "do not touch".

    `strawberry.UNSET` is the default on every field so that the three states
    a partial update needs stay distinct -- set to a value, set to null, and
    not mentioned. With a plain `None` default the last two collapse and there
    is no request that clears a grouping.

    `name`, `layout` and `visibility` are declared nullable even though their
    columns are NOT NULL and "cleared" has no meaning for them. That is not
    the schema mis-describing them; it is the only shape GraphQL offers. An
    input field is required exactly when it is non-null and carries no
    default, so declaring them non-null makes them mandatory on every patch --
    which is the opposite of a patch -- and giving them a default makes an
    omission silently overwrite the stored value with it. The explicit null
    they let through is refused by SavedViewService as a field error rather
    than reaching a NOT NULL violation the client cannot read.

    Sharing is `visibility: SHARED` and has no mutation of its own. It is one
    column on one row, so a separate `savedViewShare` would be a second write
    path to the same value -- and a client that renamed and shared in one
    gesture would have to send two mutations that can half-fail.
    """

    workspace_slug: str
    id: UUID

    name: str | None = strawberry.UNSET

    team_id: UUID | None = strawberry.UNSET

    # An explicit null clears the filter -- the view becomes every issue in
    # the workspace -- which is a real edit and not a mistake. Omitting the
    # field leaves the stored filter alone.
    filter: IssueFilterInput | None = strawberry.UNSET
    order_by: IssueOrderInput | None = strawberry.UNSET

    layout: SavedViewLayoutType | None = strawberry.UNSET
    grouping: SavedViewGroupingType | None = strawberry.UNSET
    subgrouping: SavedViewGroupingType | None = strawberry.UNSET
    visibility: SavedViewVisibilityType | None = strawberry.UNSET


@strawberry.input
class SavedViewDeleteInput:
    workspace_slug: str
    id: UUID


@strawberry.input(
    description=(
        "Exactly one of `teamId`, `projectId` and `savedViewId` must be "
        "supplied. Sending none or more than one is a field error rather "
        "than a guess at which was meant."
    )
)
class FavoriteAddInput:
    """Three optional ids and a rule about them, rather than three mutations.

    One mutation because the operation is the same one -- append a pointer to
    this person's list -- and its answer is the same `Favorite`. Three
    mutations would be three payloads, three resolvers and three places to
    forget the visibility guard on saved views.

    The rule is checked in `FavoriteService.add` rather than by
    `favorites_one_target`: a client that named two targets needs to be told
    which field to fix, and a CheckViolationError carrying a rendered
    constraint says nothing a UI can act on. The constraint is still there,
    for writes that do not come through this code.
    """

    workspace_slug: str

    team_id: UUID | None = None
    project_id: UUID | None = None
    saved_view_id: UUID | None = None


@strawberry.input
class FavoriteRemoveInput:
    workspace_slug: str

    # The favourite's own id, not the id of the thing it points at. A
    # favourite is a row in one person's list and that row is what is being
    # removed; keying on the target would need the kind alongside it and would
    # make "un-favourite" a lookup rather than a delete.
    id: UUID


@strawberry.input
class FavoriteReorderInput:
    workspace_slug: str
    id: UUID

    # Where to put it. Positions are neither unique nor required to be
    # contiguous -- see the column note in migration 019 -- so a client
    # dragging an item between two others sends any number between them, and
    # one that has run out of room renumbers by calling this once per item.
    position: int
