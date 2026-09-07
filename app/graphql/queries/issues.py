from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.domain.issues import DEFAULT_ORDER, NO_FILTER
from app.graphql.errors import bad_user_input
from app.graphql.inputs.issue import IssueFilterInput, IssueOrderInput
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
        filter: IssueFilterInput | None = None,
        order_by: IssueOrderInput | None = None,
        first: int = DEFAULT_FIRST,
        after: str | None = None,
    ) -> IssueConnection:
        """A keyset page of one workspace's live issues.

        One `filter` object rather than eight loose arguments, which is not
        only tidiness: a filter has to travel to `totalCount` and into the
        cursor validation as one thing, and eight parameters threaded through
        three layers is eight chances to drop one on the way.

        Every field of that filter NARROWS. The workspace is resolved from
        the slug through the membership check and leads the statement either
        way, so a `projectId` or `cycleId` from another workspace matches no
        rows instead of selecting that workspace's -- the same empty page an
        id naming nothing at all gets. Omitting the filter entirely is the
        whole workspace, which is what an "all issues" screen asks for.

        `orderBy` defaults to newest first, the order this field has always
        returned, so an existing document keeps its existing pages. A cursor
        carries the ordering it was minted under and this refuses one minted
        under a different one, because resuming a keyset walk in a different
        sort does not fail -- it silently returns the wrong rows.
        """
        scope = await authorized_scope(info, workspace_slug)

        issue_filter = NO_FILTER if filter is None else filter.to_filter()
        order = DEFAULT_ORDER if order_by is None else order_by.to_order()

        try:
            page = await info.context.issue_service.list(
                scope=scope,
                issue_filter=issue_filter,
                order=order,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected input errors are translated. Anything else
            # (asyncpg failures, bugs) propagates as a real execution error.
            # `from None` keeps parser detail out of the response.
            raise bad_user_input("Invalid issue list arguments", exc) from None

        return IssueConnection.from_domain(page, scope, issue_filter)
