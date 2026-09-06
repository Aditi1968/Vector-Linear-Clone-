from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.types.project import ProjectConnection, ProjectType


DEFAULT_FIRST = 50


@strawberry.type
class ProjectQuery:
    """The read half of the projects API.

    Merged into the schema's single `Query` by app/graphql/root.py. Kept as
    its own class so that the fields of a feature live with that feature and
    two people adding queries at once are not editing the same class.
    """

    @strawberry.field
    async def project(self, info: Info, id: UUID) -> ProjectType | None:
        """One project, or null.

        The workspace comes from the request, never from the document -- see
        app/graphql/tenancy.py for where it comes from today. A project in
        another workspace resolves to null, the same answer as an id that
        exists nowhere, so this cannot be used to ask whether someone else's
        project exists.
        """
        scope = await info.context.tenant.scope()

        entity = await info.context.project_service.get_by_id(
            scope=scope,
            project_id=id,
        )

        if entity is None:
            return None

        return ProjectType.from_entity(entity)

    @strawberry.field
    async def projects(
        self,
        info: Info,
        first: int = DEFAULT_FIRST,
        after: str | None = None,
    ) -> ProjectConnection:
        scope = await info.context.tenant.scope()

        try:
            page = await info.context.project_service.list(
                scope=scope,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected pagination input errors are translated. Anything
            # else (asyncpg failures, bugs) propagates as a real execution
            # error. `from None` keeps parser detail out of the response.
            #
            # The same GraphQLError shape the issues query raises, because it
            # is the same failure: a client cannot be expected to learn two
            # error vocabularies for one concept.
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

        return ProjectConnection.from_domain(page)
