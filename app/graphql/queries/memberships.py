from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import WorkspaceAccessDeniedError
from app.graphql.types.membership import WorkspaceMembershipType


# The two messages these resolvers publish, as constants because they are a
# contract rather than prose. Both codes are in
# `app.graphql.schema.PUBLIC_ERROR_CODES`, which means every message ever
# raised under them reaches clients verbatim.
UNAUTHENTICATED_MESSAGE = "Authentication required"

# One message for a workspace that does not exist and for one the viewer does
# not belong to. Phrased as a miss rather than a refusal, because a refusal
# would confirm the workspace is real. The server does not know which case it
# is in -- see WorkspaceAccessDeniedError -- so this is the only answer it
# could give truthfully anyway.
WORKSPACE_NOT_FOUND_MESSAGE = "Workspace not found"


def _viewer_user_id(info: Info) -> UUID:
    """The authenticated viewer, or an UNAUTHENTICATED error.

    Fails closed and fails first. `viewer_user_id` is None until the session
    layer populates it, so today every protected field below answers
    UNAUTHENTICATED -- which is the correct answer for a server that cannot
    yet identify anyone, and the direction a half-built auth stack must fail
    in. The alternative shapes are both accidents waiting: reading a user id
    out of a field argument would let any caller name any user, and treating
    an absent viewer as "no memberships" would answer an unauthenticated
    stranger with an empty list, which reads to a client exactly like a
    successfully authenticated account that belongs to nothing.

    Raised before the service is touched, so an unauthenticated request
    performs no protected data lookup at all.
    """
    viewer_user_id = info.context.viewer_user_id

    if viewer_user_id is None:
        raise GraphQLError(
            UNAUTHENTICATED_MESSAGE,
            extensions={"code": "UNAUTHENTICATED"},
        )

    # Annotated rather than returned inline: the context attribute is untyped
    # here, so returning it directly would satisfy any return type.
    identified: UUID = viewer_user_id

    return identified


@strawberry.type
class MembershipQuery:
    """Root query fields for workspace membership.

    A class of its own, merged into the schema's root `Query` in
    app.graphql.schema, so that the issues resolvers and these can be read --
    and reviewed -- without one file's fields sitting in the other's module.

    Both fields are scoped to the viewer and neither takes a user id. That is
    the tenancy rule at the transport layer: a workspace slug is a public
    string that anyone can type, so it may select what is being asked about
    and never who is asking.
    """

    @strawberry.field
    async def my_workspaces(self, info: Info) -> list[WorkspaceMembershipType]:
        """Every workspace the viewer belongs to, with the role they hold.

        No arguments at all, including no `first`. The list is bounded by
        `app.services.memberships.MEMBERSHIP_LIST_LIMIT` on the server rather
        than by the client; see that constant for why an unpaginated field is
        the right shape for this list today and what changes when it is not.

        Note for `app.graphql.limits`: this field declares no page-size
        argument, so the complexity rule charges it once. That stays true only
        while the list is small by construction. A `first` argument added here
        must be added to PAGE_SIZE_ARGUMENTS' reasoning too.
        """
        user_id = _viewer_user_id(info)

        memberships = await info.context.membership_service.list_for_user(
            user_id=user_id
        )

        return [WorkspaceMembershipType.from_entity(entity) for entity in memberships]

    @strawberry.field
    async def my_workspace(self, info: Info, slug: str) -> WorkspaceMembershipType:
        """The viewer's membership of one workspace, by slug.

        Non-null, and it errors rather than returning null on a miss. A
        nullable field would make "no such workspace" and "not yours"
        indistinguishable too, but it would also make them indistinguishable
        from a workspace the client asked for wrongly, and a client cannot act
        on that. The error carries a NOT_FOUND code and one fixed message for
        both cases.

        The slug is passed to the service exactly as the client wrote it --
        not trimmed, lowercased or otherwise normalised. Normalising here
        would mean two spellings of a slug address one tenant, which is what
        `workspaces_slug_format` exists to prevent.
        """
        user_id = _viewer_user_id(info)

        try:
            membership = await info.context.membership_service.membership_for_slug(
                slug=slug,
                user_id=user_id,
            )
        except WorkspaceAccessDeniedError:
            # Only this one expected refusal is translated. Anything else --
            # an asyncpg failure, a bug -- propagates and is masked, rather
            # than being reported to the client as a missing workspace.
            # `from None` keeps the domain exception out of the response.
            raise GraphQLError(
                WORKSPACE_NOT_FOUND_MESSAGE,
                extensions={"code": "NOT_FOUND"},
            ) from None

        return WorkspaceMembershipType.from_entity(membership)
