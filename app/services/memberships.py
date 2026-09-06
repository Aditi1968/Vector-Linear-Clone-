from uuid import UUID

import asyncpg

from app.domain.errors import WorkspaceAccessDeniedError
from app.domain.memberships import WorkspaceMembershipEntity
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.repositories.memberships import MembershipRepository


# The most memberships one `myWorkspaces` answers with.
#
# Not a page size and not a clamp on a user-supplied argument: no caller
# chooses it. It is the bound that keeps a single resolver from turning into
# an unbounded read if one account ever accumulates an absurd number of
# workspaces -- a scripted signup loop, a bug in an invitation flow.
#
# Set far above any real account on purpose. A person belongs to workspaces in
# the low tens, so truncation is not a case real users reach, and the honest
# reading of this constant is "a backstop", not "page one". When a real account
# does approach it, this field grows a cursor the way `issues` has one; a
# larger number here would be the wrong fix, because the problem it would be
# papering over is an unpaginated list and not a small limit.
MEMBERSHIP_LIST_LIMIT = 200


class MembershipService:
    """Business rules for workspace membership.

    This is where a caller's identity and a workspace's identity are combined
    into permission to act, and it is deliberately the only place: every
    transport that ever needs an authorization decision -- GraphQL now, REST
    or a worker later -- asks the same method rather than reimplementing the
    lookup with its own idea of what an absent row means. The service also
    owns connection acquisition and transaction boundaries.

    Nothing here trusts an argument to describe the caller. `user_id` must
    come from an authenticated session, never from a client-supplied field:
    this class cannot tell the difference, which is exactly why the boundary
    that can -- the GraphQL context -- is the only thing that supplies it.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: MembershipRepository,
    ):
        self._pool = pool
        self._repository = repository

    async def membership_for_slug(
        self,
        *,
        slug: str,
        user_id: UUID,
    ) -> WorkspaceMembershipEntity:
        """The caller's membership of one workspace, or a refusal.

        Raises WorkspaceAccessDeniedError when the lookup finds nothing, which
        covers a slug no workspace holds and a workspace this user is not a
        member of, without distinguishing them. The repository answers both
        with the same absent row from one statement, so this method is not
        collapsing two answers into one -- it never receives two. See
        WorkspaceAccessDeniedError for why that is a property of the design
        rather than of this frame.

        WorkspaceNotFoundError is deliberately not raised here. That error
        means a slug matched nothing and is entitled to say so; using it for a
        refusal would tell a non-member that the workspace exists, which is
        the leak this whole path is shaped to avoid.

        A single SELECT needs no explicit write transaction, so this acquires
        a connection without opening one, and releases it before deciding what
        the lookup means.
        """
        async with self._pool.acquire() as connection:
            membership = await self._repository.find_membership(
                connection,
                slug=slug,
                user_id=user_id,
            )

        if membership is None:
            raise WorkspaceAccessDeniedError()

        return membership

    async def authorized_scope_for_slug(
        self,
        *,
        slug: str,
        user_id: UUID,
    ) -> AuthorizedWorkspaceScope:
        """Resolve (slug, caller) to the scope tenant-owned work is bound to.

        This is the function that turns a string off the wire into permission.
        Everything downstream that must not be reachable by a non-member takes
        the AuthorizedWorkspaceScope it returns, so "was this checked?" is
        answered by a type rather than by reading call sites: a
        `WorkspaceScope` cannot be passed where one of these is annotated,
        and one of these can only be built from a row in `workspace_members`.

        Both id fields come from the row rather than from the arguments. The
        user id read back is the one the database matched, so the scope
        describes what was found and not what was asked for -- a distinction
        that costs nothing here and stops being free the moment this lookup
        grows a join, an alias table, or an impersonation path.

        The raw slug reaches exactly two frames: this call and the lookup it
        delegates to. Past this point tenant-owned work is handed a scope and
        never the string it came from.
        """
        membership = await self.membership_for_slug(slug=slug, user_id=user_id)

        return AuthorizedWorkspaceScope(
            workspace_id=membership.workspace_id,
            user_id=membership.user_id,
            role=membership.role,
        )

    async def list_for_user(
        self,
        *,
        user_id: UUID,
    ) -> list[WorkspaceMembershipEntity]:
        """Every workspace this user belongs to, bounded by the service.

        There is no workspace filter and no way to ask for someone else's
        list. The user id is the whole query, so the only way to read another
        account's workspaces is to be authenticated as that account.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list_for_user(
                connection,
                user_id=user_id,
                limit=MEMBERSHIP_LIST_LIMIT,
            )
