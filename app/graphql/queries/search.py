from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError, WorkspaceAccessDeniedError
from app.graphql.queries.memberships import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.scope import authorized_scope
from app.graphql.types.search import DuplicateSuggestionType, SearchResultsType
from app.graphql.viewer import viewer_user_id


DEFAULT_FIRST = 20

# A shorter default than `search`, deliberately. This list is shown beside a
# form somebody is still filling in, so it competes with the thing they came to
# do; five candidates is a glance and twenty is an interruption.
DEFAULT_DUPLICATE_FIRST = 5


def _invalid(exc: ValidationError, message: str) -> GraphQLError:
    """The one masked-input error this module raises, built once.

    BAD_USER_INPUT is in `app.graphql.schema.PUBLIC_ERROR_CODES`, so everything
    in `extensions` reaches the client verbatim -- which is the reason it is
    built from `ValidationIssue` fields and never from an exception's own text.
    """
    return GraphQLError(
        message,
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
    )


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
            raise _invalid(exc, "Invalid search arguments") from None

        return SearchResultsType.from_domain(results, scope)

    @strawberry.field
    async def issue_duplicate_suggestions(
        self,
        info: Info,
        workspace_slug: str,
        title: str,
        description: str | None = None,
        exclude_issue_id: UUID | None = None,
        first: int = DEFAULT_DUPLICATE_FIRST,
    ) -> list[DuplicateSuggestionType]:
        """Issues in this workspace that might already be the one being written.

        Takes the TEXT and not an issue id, so one field serves both moments it
        is wanted: while somebody is typing an issue that does not exist yet,
        and while somebody is editing one that does. `excludeIssueId` is what
        separates them -- passed when editing, so the issue is not offered as a
        duplicate of itself.

        Authorized before anything is read, through the same
        `authorized_scope` every workspace-scoped field uses: the slug is a
        public string that may select what is being asked about and never who
        is asking, the viewer comes from the session cookie, and a slug the
        viewer does not belong to never reaches the service. That scope becomes
        a `workspace_id` equality inside every arm of the SQL -- not a filter
        over a top-k, which for a nearest-neighbour read would be the whole
        vulnerability; see `EmbeddingRepository` and migration 025.

        `excludeIssueId` is NOT authorized separately, and does not need to be:
        it only ever REMOVES a row from a list already scoped to this
        workspace, so an id from another tenant excludes nothing and the answer
        is identical to having passed none. There is no id here whose presence
        or absence in the result could be observed.

        Empty when this deployment has no embedder wired. That is the honest
        answer rather than a lexical impostor -- see
        `SearchService.suggest_duplicates` -- and it is deliberately not
        distinguishable from "nothing similar was found": both are "no
        suggestions", and a client that rendered them differently would be
        rendering a fact about the server's configuration.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            suggestions = await info.context.search_service.suggest_duplicates(
                scope=scope,
                title=title,
                description=description,
                exclude_issue_id=exclude_issue_id,
                first=first,
            )
        except ValidationError as exc:
            raise _invalid(exc, "Invalid duplicate suggestion arguments") from None

        return [
            DuplicateSuggestionType.from_domain(suggestion, scope)
            for suggestion in suggestions
        ]
