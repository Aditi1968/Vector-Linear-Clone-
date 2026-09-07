import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.errors import bad_user_input
from app.graphql.scope import authorized_scope
from app.graphql.types.notification import (
    DEFAULT_NOTIFICATION_FIRST,
    NotificationConnection,
)


@strawberry.type
class NotificationQuery:
    """The read half of the inbox, merged into the root Query.

    Both fields take a `workspaceSlug` and neither takes a user, and that
    asymmetry is the whole tenancy rule at this layer: a slug is a public
    string anyone can type, so it may select WHAT is being asked about and
    never WHO is asking. The answer is always the authenticated viewer's own
    inbox in a workspace they belong to -- there is no argument through which
    a caller could ask for anybody else's, in this workspace or another.

    Both begin by establishing identity before touching data. An
    unauthenticated request is refused while it is still just a cookie that
    named nothing, so no protected lookup happens for it at all.

    These are the first fields in the schema to name a workspace explicitly
    rather than going through `app/graphql/tenancy.py`'s bootstrap constant.
    That is not an inconsistency to fix here: an inbox is per (person,
    workspace) and cannot be answered from an ambient tenant at all, so this
    feature needs the argument the rest of the schema is still growing.
    """

    @strawberry.field
    async def notifications(
        self,
        info: Info,
        workspace_slug: str,
        unread_only: bool = False,
        first: int = DEFAULT_NOTIFICATION_FIRST,
        after: str | None = None,
    ) -> NotificationConnection:
        """The viewer's notifications in one workspace, newest first.

        `unreadOnly` defaults to false, so the unqualified field is the whole
        inbox. The default is the safer one to be wrong about: a client that
        wanted everything and got only the unread would silently lose the
        history half of the list.
        """
        scope = await authorized_scope(info, workspace_slug)

        try:
            page = await info.context.activity_service.list_notifications(
                scope=scope,
                unread_only=unread_only,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected pagination input is translated. Anything else --
            # an asyncpg failure, a bug -- propagates as the real execution
            # error it is and is masked on the way out.
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return NotificationConnection.from_domain(page, scope)

    @strawberry.field
    async def notification_unread_count(
        self,
        info: Info,
        workspace_slug: str,
    ) -> int:
        """How many unread notifications the viewer has in one workspace.

        A field of its own rather than a `totalCount` on the connection
        above, because it is the query a badge actually runs: it needs no
        page, no cursor and no rows, and folding it into the connection would
        make every inbox read pay for a count it usually does not want.
        """
        scope = await authorized_scope(info, workspace_slug)

        # Annotated rather than returned inline: the context attribute is
        # untyped here, so returning it directly would satisfy any return type.
        count: int = await info.context.activity_service.unread_count(scope=scope)

        return count
