import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import WorkspaceAccessDeniedError
from app.graphql.scope import WORKSPACE_NOT_FOUND_MESSAGE, authorized_scope
from app.graphql.types.invitation import WorkspaceInvitationType
from app.graphql.types.membership import WorkspaceMembershipType, WorkspaceMemberType
from app.graphql.viewer import viewer_user_id


# WORKSPACE_NOT_FOUND_MESSAGE is imported rather than declared here now that
# every workspace-scoped resolver raises it. It moved to app/graphql/scope.py,
# beside the helper that raises it everywhere else, so that this field and the
# whole scoped API cannot answer a non-member two different ways.


def workspace_not_found() -> GraphQLError:
    """The one refusal every workspace-scoped field answers with.

    Raised for a slug that matches nothing, for a workspace the viewer is not
    a member of, and for one they are a member of but may not administer. The
    first two are indistinguishable to the server -- see
    WorkspaceAccessDeniedError -- and the third is folded in on purpose, so
    that "list this workspace's invitations" does not tell an ordinary member
    the difference between a workspace they cannot administer and one they
    cannot see.

    Raise it `from None`. The domain exception it describes carries nothing a
    client may read, and chaining it would attach an `original_error`, which
    is exactly what `app.graphql.schema.is_public_error` reads to decide an
    error was an accident and must be masked.
    """
    return GraphQLError(
        WORKSPACE_NOT_FOUND_MESSAGE,
        extensions={"code": "NOT_FOUND"},
    )


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
            raise workspace_not_found() from None

        return WorkspaceMembershipType.from_entity(membership)

    @strawberry.field(
        description=("Everyone in a workspace, with the role each holds. Members only.")
    )
    async def workspace_members(
        self, info: Info, workspace_slug: str
    ) -> list[WorkspaceMemberType]:
        """The workspace's people, for an assignee picker or a settings page.

        Errors rather than answering an empty list for a workspace the viewer
        cannot see, which is the opposite of what `teams` does today and is
        the right shape here: an empty member list is a real answer for
        nothing (every workspace has at least its owner), so returning one
        would be inventing a state the product does not have.

        Bounded server-side by `app.services.memberships.MEMBERSHIP_LIST_LIMIT`
        and declaring no page-size argument, so `app.graphql.limits` charges it
        once. Adding a `first` here means revisiting PAGE_SIZE_ARGUMENTS.
        """
        scope = await authorized_scope(info, workspace_slug)

        members = await info.context.membership_service.list_members(scope=scope)

        return [WorkspaceMemberType.from_entity(entity) for entity in members]

    @strawberry.field(
        description=(
            "Invitations to this workspace that have not been accepted or "
            "expired. Admins and owners only."
        )
    )
    async def invitations(
        self, info: Info, workspace_slug: str
    ) -> list[WorkspaceInvitationType]:
        """Outstanding invitations, for the settings page that manages them.

        Narrower than `workspaceMembers`: a pending invitation discloses an
        address belonging to someone who has not joined, so the service
        refuses an ordinary member -- with the same refusal a stranger gets,
        which is why both arrive here as one exception.
        """
        scope = await authorized_scope(info, workspace_slug)

        invitations = await info.context.membership_service.list_invitations(
            scope=scope
        )

        return [WorkspaceInvitationType.from_entity(entity) for entity in invitations]
