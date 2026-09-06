from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.scope import authorized_scope
from app.graphql.types.issue import IssueConnection, IssueType


DEFAULT_FIRST = 50


@strawberry.type
class Query:
    @strawberry.field
    async def issue(
        self,
        info: Info,
        workspace_slug: str,
        id: UUID,
    ) -> IssueType | None:
        """One live issue from a workspace the viewer belongs to, or null.

        Two arguments and both are required, in this order, because they are
        checked in this order: the slug decides whether the caller may look at
        all, and only then does the id select a row. An issue whose id belongs
        to another workspace resolves to null -- the same answer as an id that
        exists nowhere -- so a caller cannot pair a leaked id with their own
        slug to confirm that the issue is real.
        """
        scope = await authorized_scope(info, workspace_slug)

        entity = await info.context.issue_service.get_by_id(scope=scope, issue_id=id)

        if entity is None:
            return None

        return IssueType.from_entity(entity, scope)

    @strawberry.field
    async def issues(
        self,
        info: Info,
        workspace_slug: str,
        team_id: UUID | None = None,
        first: int = DEFAULT_FIRST,
        after: str | None = None,
    ) -> IssueConnection:
        """A keyset page of one workspace's live issues, newest first.

        `teamId` is optional and filters within the workspace rather than
        widening it: the tenant predicate leads the statement either way, so a
        team id from another workspace matches no rows instead of selecting
        that workspace's. Omitting it is the whole workspace, which is what an
        "all issues" screen asks for.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            page = await info.context.issue_service.list(
                scope=scope,
                team_id=team_id,
                first=first,
                after=after,
            )
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

        return IssueConnection.from_domain(page, scope)
