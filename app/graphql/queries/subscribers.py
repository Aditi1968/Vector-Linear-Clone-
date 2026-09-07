from uuid import UUID

import strawberry
from strawberry.types import Info

from app.graphql.scope import authorized_scope
from app.graphql.types.subscriber import IssueSubscriberType


@strawberry.type
class SubscriberQuery:
    """The read half of watching, merged into the root Query.

    Root fields rather than fields on `Issue`, and that is a scope decision
    rather than a style one: an issue's watchers are read when a person opens
    ONE issue, never once per row of a list, so hanging them off the issue type
    would put a per-row query on every list page that happened to select them.
    `app.graphql.limits` charges a root field once, which is what this costs.

    Both fields establish identity before touching data -- `authorized_scope`
    resolves the viewer first, so an unauthenticated request is refused while
    it is still just a cookie that named nothing.
    """

    @strawberry.field
    async def issue_subscribers(
        self,
        info: Info,
        workspace_slug: str,
        issue_id: UUID,
    ) -> list[IssueSubscriberType]:
        """Everyone watching one issue, oldest subscription first.

        No connection and no cursor: the set is bounded by the workspace's
        membership rather than growing with use, so it is returned whole. See
        `app.services.activity.SUBSCRIBERS_MAX` for the ceiling and for what
        would have to change if a workspace ever reached it.

        An issue in another workspace answers an empty list, exactly as an
        issue nobody watches does. The equivalence is the isolation property:
        any distinguishable answer would confirm that a guessed id names a real
        issue.
        """
        scope = await authorized_scope(info, workspace_slug)

        subscribers = await info.context.activity_service.list_subscribers(
            scope=scope,
            issue_id=issue_id,
        )

        return [IssueSubscriberType.from_entity(entity) for entity in subscribers]

    @strawberry.field
    async def issue_viewer_is_subscribed(
        self,
        info: Info,
        workspace_slug: str,
        issue_id: UUID,
    ) -> bool:
        """Whether the viewer is watching one issue.

        A field of its own rather than a client searching `issueSubscribers`
        for its own id, because that is the question a toggle asks and it needs
        one boolean rather than a list. It also answers correctly for a
        workspace big enough for the list above to be truncated.

        There is no user argument. The answer is about the authenticated viewer
        and nothing else -- a field that took one would report whether a named
        person is watching a named issue, which is a fact about them rather
        than about the issue.
        """
        scope = await authorized_scope(info, workspace_slug)

        subscribed: bool = await info.context.activity_service.is_subscribed(
            scope=scope,
            issue_id=issue_id,
        )

        return subscribed
