import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError, WorkspaceAccessDeniedError
from app.graphql.queries.memberships import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.types.search import SearchResultsType
from app.graphql.viewer import viewer_user_id


DEFAULT_FIRST = 20


@strawberry.type
class SearchQuery:
    """The one root field application search exposes.

    Its own class, merged into the schema's single `Query` by
    app/graphql/schema.py, so that the feature's field lives with the feature.
    """

    @strawberry.field
    async def search(
        self,
        info: Info,
        workspace_slug: str,
        query: str,
        first: int = DEFAULT_FIRST,
    ) -> SearchResultsType:
        """Search one workspace's issues and projects.

        The workspace is named by the caller and authorized before anything is
        read. `workspace_slug` is a public string anyone can type, so it may
        select what is being asked about and never who is asking: the viewer
        comes from the session cookie, and `authorized_scope_for_slug` is what
        turns the pair into an AuthorizedWorkspaceScope. A slug the viewer does
        not belong to never reaches the search at all, and the workspace id
        that does reach it came out of `workspace_members` rather than off the
        wire.

        That scope becomes a `workspace_id` equality in the WHERE clause of
        every statement the service issues. It is not a filter applied to
        results: another tenant's issue is never read into this process, so
        there is nothing for a post-filter to be forgotten about.

        A workspace that does not exist and one the viewer may not see are the
        same NOT_FOUND, carrying the same message `myWorkspace` uses --
        imported rather than retyped, because two spellings of it would be two
        answers a client could tell apart, which is the leak the single message
        exists to close.

        `first` is not clamped here. The service owns the bound and reports it
        as a structured error, so a client asking for too many is told so once
        rather than being silently served a different request.
        """
        user_id = await viewer_user_id(info)

        try:
            scope = await info.context.membership_service.authorized_scope_for_slug(
                slug=workspace_slug,
                user_id=user_id,
            )
        except WorkspaceAccessDeniedError:
            # Only this one expected refusal is translated. Anything else --
            # an asyncpg failure, a bug -- propagates and is masked rather than
            # being reported to the client as a missing workspace. `from None`
            # keeps the domain exception out of the response.
            raise GraphQLError(
                WORKSPACE_NOT_FOUND_MESSAGE,
                extensions={"code": "NOT_FOUND"},
            ) from None

        try:
            results = await info.context.search_service.search(
                scope=scope,
                query=query,
                first=first,
            )
        except ValidationError as exc:
            raise GraphQLError(
                "Invalid search arguments",
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

        return SearchResultsType.from_domain(results)
