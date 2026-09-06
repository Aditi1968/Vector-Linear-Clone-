import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import WorkspaceAccessDeniedError
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE
from app.graphql.types.membership import WorkspaceMembershipType
from app.graphql.viewer import viewer_user_id


# WORKSPACE_NOT_FOUND_MESSAGE is imported rather than declared here now that
# every workspace-scoped resolver raises it. It moved to app/graphql/scope.py,
# beside the helper that raises it everywhere else, so that this field and the
# whole scoped API cannot answer a non-member two different ways.


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
        user_id = await viewer_user_id(info)

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
        user_id = await viewer_user_id(info)

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
