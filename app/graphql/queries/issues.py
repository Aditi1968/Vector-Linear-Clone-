from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.types.issue import IssueConnection, IssueType


DEFAULT_FIRST = 50


@strawberry.type
class Query:
    @strawberry.field
    async def issue(self, info: Info, id: UUID) -> IssueType | None:
        entity = await info.context.issue_service.get_by_id(id)

        if entity is None:
            return None

        return IssueType.from_entity(entity)

    @strawberry.field
    async def issues(
        self,
        info: Info,
        first: int = DEFAULT_FIRST,
        after: str | None = None,
    ) -> IssueConnection:
        try:
            page = await info.context.issue_service.list(first=first, after=after)
        except ValidationError as exc:
            # Only expected pagination input errors are translated. Anything
            # else (asyncpg failures, bugs) propagates as a real execution
            # error. `from None` keeps parser detail out of the response.
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

        return IssueConnection.from_domain(page)
