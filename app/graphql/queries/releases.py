from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.scope import authorized_scope
from app.graphql.types.release import (
    EnvironmentType,
    ReleaseConnection,
    ReleaseType,
)


DEFAULT_FIRST = 50


@strawberry.type
class ReleaseQuery:
    """The read half of the releases API.

    Merged into the schema's single `Query` by app/graphql/schema.py. Kept as
    its own class so that the fields of a feature live with that feature and
    two people adding queries at once are not editing the same class.
    """

    @strawberry.field
    async def environments(
        self,
        info: Info,
        workspace_slug: str,
    ) -> list[EnvironmentType]:
        """This workspace's deploy targets, by name.

        Unpaginated, and that is a decision rather than an omission: a
        workspace has a handful of environments and this field feeds a
        dropdown. A connection here would be API surface for a scroll nobody
        performs.
        """
        scope = await authorized_scope(info, workspace_slug)

        entities = await info.context.release_service.list_environments(scope=scope)

        return [EnvironmentType.from_entity(entity) for entity in entities]

    @strawberry.field
    async def release(
        self,
        info: Info,
        workspace_slug: str,
        id: UUID,
    ) -> ReleaseType | None:
        """One release, or null.

        The slug is authorized before the id selects anything. A release in
        another workspace resolves to null, the same answer as an id that
        exists nowhere, so this cannot be used to ask whether someone else's
        release exists.
        """
        scope = await authorized_scope(info, workspace_slug)

        entity = await info.context.release_service.get_by_id(
            scope=scope,
            release_id=id,
        )

        if entity is None:
            return None

        return ReleaseType.from_entity(entity)

    @strawberry.field
    async def releases(
        self,
        info: Info,
        workspace_slug: str,
        first: int = DEFAULT_FIRST,
        after: str | None = None,
    ) -> ReleaseConnection:
        """A keyset page of the workspace's releases, newest first.

        Every environment's releases in one list, ordered by when they were
        cut. A per-environment filter is the obvious next argument and is
        deliberately not here yet: `releases_workspace_environment_idx` exists
        for it, so adding one is an argument and a predicate rather than a
        schema change.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            page = await info.context.release_service.list(
                scope=scope,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected pagination input errors are translated. Anything
            # else (asyncpg failures, bugs) propagates as a real execution
            # error. `from None` keeps parser detail out of the response.
            #
            # The same GraphQLError shape the issues and initiatives queries
            # raise, because it is the same failure: a client cannot be
            # expected to learn a new error vocabulary per feature.
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

        return ReleaseConnection.from_domain(page)
