from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.errors import bad_user_input
from app.graphql.inputs.saved_view import patched
from app.graphql.scope import authorized_scope
from app.graphql.types.saved_view import (
    FavoriteType,
    SavedViewConnection,
    SavedViewType,
)


DEFAULT_FIRST = 50


@strawberry.type
class SavedViewQuery:
    """The read half of the saved views and favorites API.

    Merged into the schema's single `Query` by app/graphql/schema.py. Kept as
    its own class so that the fields of a feature live with that feature and
    two people adding queries at once are not editing the same class.
    """

    @strawberry.field
    async def saved_view(
        self,
        info: Info,
        workspace_slug: str,
        id: UUID,
    ) -> SavedViewType | None:
        """One saved view, or null.

        The slug is authorized before the id selects anything, and the viewer
        that authorization produced is part of the lookup rather than a check
        applied afterwards. A view in another workspace, a view that does not
        exist, and another member's personal view all resolve to null -- so
        this cannot be used to ask whether somebody else has a private view
        with a given id.

        Loading the view's ISSUES is `savedView { issues { ... } }` in the
        same document. The filter comes off the stored row inside that
        resolver and never from the wire; see SavedViewType.issues.
        """
        scope = await authorized_scope(info, workspace_slug)

        entity = await info.context.saved_view_service.get_by_id(
            scope=scope,
            saved_view_id=id,
        )

        if entity is None:
            return None

        return SavedViewType.from_entity(entity, scope)

    @strawberry.field
    async def saved_views(
        self,
        info: Info,
        workspace_slug: str,
        team_id: UUID | None = strawberry.UNSET,
        first: int = DEFAULT_FIRST,
        after: str | None = None,
    ) -> SavedViewConnection:
        """A keyset page of the views this viewer may read, by name.

        `teamId` has three states and needs all three: omitted is every view
        in the workspace, an id is that team's views, and an explicit null is
        the workspace-wide ones -- the views filed under no team at all. That
        is the same spelling `IssueFilterInput` uses for the filters whose
        column is nullable, and for the same reason: a nullable argument
        already spends `null` on "no filter", so the distinction has to come
        from the difference between an absent field and a null one.

        The catch is worth naming, as `IssueFilterInput` names it: a client
        that passes a nullable variable straight through -- `teamId: $maybe`
        with nothing selected -- asks for the workspace-wide views rather than
        for all of them. Send the argument only when narrowing.

        Shared views and this viewer's own personal ones, and nothing else.
        The predicate is in the statement rather than applied to the result,
        so `first` counts rows this viewer may see -- a page is never short
        because it silently dropped somebody else's private views.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            page = await info.context.saved_view_service.list(
                scope=scope,
                # `patched`, not the `_present` that inputs/issue.py uses for
                # the NOT NULL filters: an explicit null SURVIVES as None
                # here, because `teamId: null` is the request for the views
                # filed under no team -- a real set of rows rather than the
                # absence of a narrowing.
                team_id=patched(team_id),
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected pagination input errors are translated. Anything
            # else (asyncpg failures, bugs) propagates as a real execution
            # error. `from None` keeps parser detail out of the response.
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return SavedViewConnection.from_domain(page, scope)

    @strawberry.field(
        description=(
            "This viewer's favorites in this workspace, in their own order. "
            "Per-user and per-workspace: the same person in two workspaces "
            "has two independent lists."
        )
    )
    async def favorites(self, info: Info, workspace_slug: str) -> list[FavoriteType]:
        """Not paginated, and that is a product statement rather than an
        oversight: a sidebar is a handful of rows a client renders whole, so a
        cursor would buy a round trip and a `hasNextPage` nobody reads. The
        service states the backstop bound; see FAVORITE_LIST_LIMIT.
        """
        scope = await authorized_scope(info, workspace_slug)

        entities = await info.context.favorite_service.list(scope=scope)

        return [FavoriteType.from_entity(entity) for entity in entities]
