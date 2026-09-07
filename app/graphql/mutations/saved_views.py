from enum import Enum

import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.domain.issues import DEFAULT_ORDER, NO_FILTER, IssueFilter, IssueOrder
from app.domain.patch import UNSET, UnsetType
from app.graphql.inputs.issue import IssueFilterInput, IssueOrderInput
from app.graphql.inputs.saved_view import (
    FavoriteAddInput,
    FavoriteRemoveInput,
    FavoriteReorderInput,
    SavedViewCreateInput,
    SavedViewDeleteInput,
    SavedViewUpdateInput,
    patched,
)
from app.graphql.scope import authorized_scope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.saved_view import (
    FavoriteDeletePayload,
    FavoritePayload,
    FavoriteType,
    SavedViewDeletePayload,
    SavedViewPayload,
    SavedViewType,
)


def _enum_value(value: Enum | None | UnsetType) -> str | None | UnsetType:
    """A GraphQL enum member as the string the column stores.

    All three cases survive: UNSET stays UNSET, an explicit null stays None
    (which the service refuses for the columns that are NOT NULL), and a real
    member becomes its value. Collapsing any pair would make a patch that
    clears a grouping indistinguishable from one that does not mention it.
    """
    if isinstance(value, UnsetType) or value is None:
        return value

    # Annotated rather than returned inline: `Enum.value` is typed `Any`, so
    # returning it directly would satisfy any return type -- including one
    # that had drifted away from the TEXT column behind it.
    stored: str = value.value

    return stored


def _filter_of(value: IssueFilterInput | None) -> IssueFilter:
    """An input filter as the domain one, with null meaning "no filter".

    A create that omits `filter` and one that sends null both mean the same
    view -- every issue in the workspace -- because on a create there is no
    prior value for the two to differ about.
    """
    return NO_FILTER if value is None else value.to_filter()


def _order_of(value: IssueOrderInput | None) -> IssueOrder:
    return DEFAULT_ORDER if value is None else value.to_order()


def _patched_filter(
    value: IssueFilterInput | None | UnsetType,
) -> IssueFilter | UnsetType:
    """A patched filter, where an explicit null clears it.

    Three cases collapsing into two on purpose: UNSET leaves the stored filter
    alone, and both `null` and an empty object mean the wide filter. There is
    no third thing a filter can be -- the column is NOT NULL and always holds
    an object -- so `null` is the spelling for "this view no longer narrows".
    """
    if isinstance(value, UnsetType):
        return UNSET

    return _filter_of(value)


def _patched_order(value: IssueOrderInput | None | UnsetType) -> IssueOrder | UnsetType:
    """A patched ordering, where an explicit null restores the default.

    `IssueOrderInput` has a default for both of its fields, so there is no
    partial ordering to preserve: an ordering is a field AND a direction, and
    a patch that changed one without the other would be a sort nobody asked
    for. Null therefore means "back to newest first" rather than "no
    ordering", which is not a state a list can be in.
    """
    if isinstance(value, UnsetType):
        return UNSET

    return _order_of(value)


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class SavedViewMutation:
    """The write half of the saved views API.

    Every resolver here has the same three lines of shape: resolve the tenant,
    call one service method, translate a ValidationError into the payload.
    Nothing decides anything -- who may edit a view, what a legal layout is,
    what happens to a favourite when the view it points at is deleted --
    because all of that is in SavedViewService, where REST and workers reach
    it too.

    `except ValidationError` and nothing broader. An asyncpg failure, a bug or
    an outage propagates through GraphQL's normal error mechanism and is
    masked by app/graphql/schema.py; converting it into a field error would
    tell a client to fix input that was never the problem, behind a 200.
    """

    @strawberry.mutation
    async def saved_view_create(
        self,
        info: Info,
        input: SavedViewCreateInput,
    ) -> SavedViewPayload:
        """Save the current list as a named view.

        `input.filter` is the same `IssueFilterInput` the client just sent to
        `issues`, so "save this list" is literally the arguments of the list
        being saved. The service validates it with the same function
        `IssueService.list` runs, which is what makes a saved view one that
        can actually be loaded.
        """
        # Resolved before the try, and outside it. A missing workspace is not
        # something the client's input can be corrected to fix, so catching it
        # here would report a server-side gap as a field error.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.saved_view_service.create(
                scope=scope,
                name=input.name,
                team_id=input.team_id,
                issue_filter=_filter_of(input.filter),
                order=_order_of(input.order_by),
                layout=input.layout.value,
                grouping=None if input.grouping is None else input.grouping.value,
                subgrouping=(
                    None if input.subgrouping is None else input.subgrouping.value
                ),
                visibility=input.visibility.value,
            )
        except ValidationError as exc:
            return SavedViewPayload(saved_view=None, errors=_errors(exc))

        return SavedViewPayload(
            saved_view=SavedViewType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def saved_view_update(
        self,
        info: Info,
        input: SavedViewUpdateInput,
    ) -> SavedViewPayload:
        """Rename, re-filter, re-sort, regroup, relayout or share one view.

        All six through one mutation because they are one UPDATE of one row.
        Sharing in particular has no mutation of its own: `visibility: SHARED`
        IS the share, and a second field writing the same column would be a
        second path for it to be set from.

        A view this viewer did not create is answered with the same NOT_FOUND
        a nonexistent id gets -- see SavedViewService for why the two must
        stay indistinguishable.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.saved_view_service.update(
                scope=scope,
                saved_view_id=input.id,
                name=patched(input.name),
                team_id=patched(input.team_id),
                issue_filter=_patched_filter(patched(input.filter)),
                order=_patched_order(patched(input.order_by)),
                layout=_enum_value(patched(input.layout)),
                grouping=_enum_value(patched(input.grouping)),
                subgrouping=_enum_value(patched(input.subgrouping)),
                visibility=_enum_value(patched(input.visibility)),
            )
        except ValidationError as exc:
            return SavedViewPayload(saved_view=None, errors=_errors(exc))

        return SavedViewPayload(
            saved_view=SavedViewType.from_entity(entity, scope),
            errors=[],
        )

    @strawberry.mutation
    async def saved_view_delete(
        self,
        info: Info,
        input: SavedViewDeleteInput,
    ) -> SavedViewDeletePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            await info.context.saved_view_service.delete(
                scope=scope,
                saved_view_id=input.id,
            )
        except ValidationError as exc:
            return SavedViewDeletePayload(
                deleted_saved_view_id=None,
                errors=_errors(exc),
            )

        return SavedViewDeletePayload(deleted_saved_view_id=input.id, errors=[])


@strawberry.type
class FavoriteMutation:
    """The write half of the favorites API.

    A separate class from SavedViewMutation because a favourite points at a
    team, a project or a saved view, and only the third has anything to do
    with saved views. They share a module because the one coupling between
    them -- deleting a view drops the favourites pointing at it -- is easier
    to keep in view when both are on one screen.
    """

    @strawberry.mutation
    async def favorite_add(
        self,
        info: Info,
        input: FavoriteAddInput,
    ) -> FavoritePayload:
        """Put one thing in this viewer's sidebar.

        The target is whichever of the three ids is supplied, and supplying
        none or two is a field error rather than a guess. A target from
        another workspace is refused by PostgreSQL rather than by anything in
        this process; a personal saved view belonging to somebody else is
        refused by a guard in the same INSERT, and both are reported as the
        same NOT_FOUND on the field that named the offending id.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.favorite_service.add(
                scope=scope,
                team_id=input.team_id,
                project_id=input.project_id,
                saved_view_id=input.saved_view_id,
            )
        except ValidationError as exc:
            return FavoritePayload(favorite=None, errors=_errors(exc))

        return FavoritePayload(favorite=FavoriteType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def favorite_remove(
        self,
        info: Info,
        input: FavoriteRemoveInput,
    ) -> FavoriteDeletePayload:
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            await info.context.favorite_service.remove(
                scope=scope,
                favorite_id=input.id,
            )
        except ValidationError as exc:
            return FavoriteDeletePayload(
                deleted_favorite_id=None,
                errors=_errors(exc),
            )

        return FavoriteDeletePayload(deleted_favorite_id=input.id, errors=[])

    @strawberry.mutation
    async def favorite_reorder(
        self,
        info: Info,
        input: FavoriteReorderInput,
    ) -> FavoritePayload:
        """Move one favourite within this viewer's own list.

        One row per call, so a whole reorder is one of these per moved item.
        More round trips than a bulk mutation, and the shape that cannot
        half-apply: positions are neither unique nor contiguous, so every
        intermediate state is a valid list.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entity = await info.context.favorite_service.reorder(
                scope=scope,
                favorite_id=input.id,
                position=input.position,
            )
        except ValidationError as exc:
            return FavoritePayload(favorite=None, errors=_errors(exc))

        return FavoritePayload(favorite=FavoriteType.from_entity(entity), errors=[])
