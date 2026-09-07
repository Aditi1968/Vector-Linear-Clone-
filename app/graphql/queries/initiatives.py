from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.scope import authorized_scope
from app.graphql.types.initiative import InitiativeConnection, InitiativeType


DEFAULT_FIRST = 50


@strawberry.type
class InitiativeQuery:
    """The read half of the initiatives API.

    Merged into the schema's single `Query` by app/graphql/schema.py. Kept as
    its own class so that the fields of a feature live with that feature and
    two people adding queries at once are not editing the same class.
    """

    @strawberry.field
    async def initiative(
        self,
        info: Info,
        workspace_slug: str,
        id: UUID,
    ) -> InitiativeType | None:
        """One initiative, or null.

        The slug is authorized before the id selects anything. An initiative in
        another workspace resolves to null, the same answer as an id that
        exists nowhere, so this cannot be used to ask whether someone else's
        initiative exists.
        """
        scope = await authorized_scope(info, workspace_slug)

        entity = await info.context.initiative_service.get_by_id(
            scope=scope,
            initiative_id=id,
        )

        if entity is None:
            return None

        return InitiativeType.from_entity(entity, scope)

    @strawberry.field
    async def initiatives(
        self,
        info: Info,
        workspace_slug: str,
        first: int = DEFAULT_FIRST,
        after: str | None = None,
    ) -> InitiativeConnection:
        """A keyset page of the workspace's initiatives, newest first.

        Flat, not a tree. A client renders the hierarchy from
        `parentInitiativeId` and `childInitiativeIds`, which every node
        carries: a nested connection would either recurse in the resolver --
        one query per level, priced as one by app/graphql/limits.py -- or need
        a depth argument the schema cannot bound.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            page = await info.context.initiative_service.list(
                scope=scope,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected pagination input errors are translated. Anything
            # else (asyncpg failures, bugs) propagates as a real execution
            # error. `from None` keeps parser detail out of the response.
            #
            # The same GraphQLError shape the issues and projects queries
            # raise, because it is the same failure: a client cannot be
            # expected to learn three error vocabularies for one concept.
            raise GraphQLError(
                "Invalid pagination arguments",
                extensions={
                    "code": "BAD_USER_INPUT",
                    "issues": [
                        {
                            "field": issue.field,
                            "code": issue.code,
                            "message": issue.message,
                        }
                        for issue in exc.issues
                    ],
                },
            ) from None

        return InitiativeConnection.from_domain(page, scope)
