import re
from datetime import timedelta
from uuid import UUID

import asyncpg

from app.domain.errors import (
    ValidationError,
    ValidationIssue,
    WorkspaceAccessDeniedError,
)
from app.domain.memberships import (
    WorkspaceInvitationEntity,
    WorkspaceMemberEntity,
    WorkspaceMembershipEntity,
)
from app.domain.tenancy import (
    WORKSPACE_OWNER_ROLE,
    AuthorizedWorkspaceScope,
    require_workspace_admin,
)
from app.repositories.invitations import InvitationRepository
from app.repositories.memberships import MembershipRepository
from app.repositories.workspaces import WorkspaceRepository

# Borrowed from the registration path rather than restated, so that an address
# this service will invite is exactly an address that service will accept as an
# account. Two copies of an email rule drift into "invited successfully, cannot
# sign up".
from app.services.auth import EMAIL_MAX_LENGTH, EMAIL_PATTERN
from app.services.tokens import generate_invitation_token, hash_invitation_token


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

NAME_MAX_LENGTH = 200

# `workspaces_slug_format` in migration 002, restated so a client is told what
# is wrong with its slug instead of receiving a masked CHECK violation.
# Lowercase, no leading or trailing hyphen -- the shape that keeps a slug both
# URL-safe and unambiguous under a case-sensitive UNIQUE.
SLUG_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# One DNS label. The schema imposes no length at all, so this is the
# application's choice and not a restatement of one: a slug appears in a
# hostname-shaped URL, and 63 is where that stops being possible.
SLUG_MAX_LENGTH = 63

# How long an invitation can be redeemed for.
#
# Absolute and short, because the token is a bearer credential for a whole
# tenant that sits in an inbox: the window in which a forwarded mail, a
# compromised mailbox or a leaked link still grants access is exactly this
# value. Seven days is long enough for someone to come back from a week off
# and short enough that a link found later is inert.
INVITATION_LIFETIME = timedelta(days=7)

# The most invitations one `invitations` answers with. A backstop and not a
# page size, on the same terms as MEMBERSHIP_LIST_LIMIT above.
INVITATION_LIST_LIMIT = 200

# Constraint name -> the field error it means, for the two violations that are
# ordinary consequences of client input rather than defects. Keyed on the name
# for the reason app/services/projects.py sets out at length: two constraints
# on one statement raise the same exception class and mean different things, so
# anything not named here is re-raised and masked rather than reported to a
# client as a correctable mistake.
WORKSPACE_SLUG_UNIQUE_CONSTRAINT = "workspaces_slug_key"
MEMBERSHIP_PKEY_CONSTRAINT = "workspace_members_pkey"

# What every unusable invitation token is answered with.
#
# One issue for four situations -- no such token, a revoked one, one already
# accepted, one that expired -- and deliberately so. A client that could tell
# them apart could ask "was this string ever a real invitation here?", which
# turns the accept mutation into an oracle for guessing tokens: the answer
# "expired" confirms a hit where "not valid" would not. The repository's
# `claim` never computes the difference either, so there is nothing for a
# later refactor to leak.
INVALID_INVITATION = ValidationIssue(
    field="token",
    code="INVALID",
    message="This invitation is no longer valid",
)

# Removing or demoting the last owner would leave a tenant nobody can ever
# administer again -- no one able to invite, promote, or hand it over -- and
# recoverable only by a hand-written UPDATE against the database.
LAST_OWNER = ValidationIssue(
    field="userId",
    code="LAST_OWNER",
    message="A workspace must always have at least one owner",
)

# One message for "no such account anywhere" and "an account that is not a
# member here", because telling them apart would let anyone holding a
# workspace answer "does this user id exist?" for any id they can guess.
MEMBER_NOT_FOUND = ValidationIssue(
    field="userId",
    code="NOT_FOUND",
    message="Member not found",
)

INVITATION_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Invitation not found",
)


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
        workspaces: WorkspaceRepository,
        invitations: InvitationRepository,
    ):
        self._pool = pool
        self._repository = repository

        # Three tables, one service, because every write below spans at least
        # two of them and each of those spans has to be one transaction: a
        # workspace without its owner's membership is a tenant nobody can
        # reach, and an accepted invitation without the membership it granted
        # is a credential spent for nothing. SQL still belongs to the
        # repository that owns each table; what lives here is the boundary
        # they are written inside.
        self._workspaces = workspaces
        self._invitations = invitations

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

    async def create_workspace(
        self,
        *,
        name: str,
        slug: str,
        owner_id: UUID,
    ) -> WorkspaceMembershipEntity:
        """Create a workspace and the creating account's ownership of it.

        One transaction, because the two rows are not two results. A workspace
        with no members is a tenant no one can enter -- not by invitation
        either, since inviting requires being an admin of it -- and the only
        way out would be a hand-written INSERT. So either both land or neither
        does, and the slug it took stays free.

        `owner_id` must come from an authenticated session. This class cannot
        tell the difference between that and a client-supplied field, which is
        exactly why the GraphQL context is the only thing that supplies it.

        Returns the membership rather than the workspace, because that is what
        was created: the caller now holds a role in a tenant, and the type
        that says so is the one every other workspace read answers with.
        """
        self._validate_workspace(name=name, slug=slug)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    workspace_id = await self._workspaces.create(
                        connection,
                        slug=slug,
                        name=name.strip(),
                    )
                except asyncpg.UniqueViolationError as exc:
                    if exc.constraint_name != WORKSPACE_SLUG_UNIQUE_CONSTRAINT:
                        raise

                    # `from None`: asyncpg's error carries the offending row's
                    # values in its `detail`, and chaining it would carry them
                    # into every traceback and log line above here.
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="slug",
                                code="SLUG_TAKEN",
                                message="This workspace URL is already taken",
                            )
                        ]
                    ) from None

                return await self._repository.create(
                    connection,
                    workspace_id=workspace_id,
                    user_id=owner_id,
                    role=WORKSPACE_OWNER_ROLE,
                )

    async def list_members(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
    ) -> list[WorkspaceMemberEntity]:
        """Everyone in the workspace the caller was authorized for.

        Members only, and any role is enough: this is what an assignee picker
        reads, so a workspace whose members cannot see each other has no way
        to assign work. Taking an AuthorizedWorkspaceScope rather than a
        workspace id is the check -- one of those can only be built from a row
        in `workspace_members`.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list_members(
                connection,
                workspace_id=scope.workspace_id,
                limit=MEMBERSHIP_LIST_LIMIT,
            )

    async def update_member_role(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        user_id: UUID,
        role: str,
    ) -> WorkspaceMemberEntity:
        """Change one member's role, subject to the two rules that matter.

        Owners are handled by owners. An admin may not promote anyone to owner
        and may not touch an existing one, because either would let an admin
        award itself the role that can remove the owners -- privilege
        escalation by way of an ordinary settings screen.

        The workspace must keep an owner. The check is made against owners
        locked by `lock_owner_ids`, not against a count read beforehand; see
        that method for why a count cannot be made safe.
        """
        require_workspace_admin(scope)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                owners = await self._lock_owners(connection, scope)

                self._require_owner_to_touch_owners(
                    scope,
                    owners=owners,
                    user_id=user_id,
                    role=role,
                )

                # Only a demotion can empty the owner list. Re-setting the last
                # owner to owner is a no-op the schema is happy with, and
                # refusing it would report a rule that is not being broken.
                if (
                    role != WORKSPACE_OWNER_ROLE
                    and user_id in owners
                    and len(owners) == 1
                ):
                    raise ValidationError([LAST_OWNER])

                member = await self._repository.update_role(
                    connection,
                    workspace_id=scope.workspace_id,
                    user_id=user_id,
                    role=role,
                )

                if member is None:
                    raise ValidationError([MEMBER_NOT_FOUND])

                return member

    async def remove_member(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        user_id: UUID,
    ) -> UUID:
        """Revoke a membership, under the same two rules as a role change."""
        require_workspace_admin(scope)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                owners = await self._lock_owners(connection, scope)

                self._require_owner_to_touch_owners(
                    scope,
                    owners=owners,
                    user_id=user_id,
                )

                if user_id in owners and len(owners) == 1:
                    raise ValidationError([LAST_OWNER])

                removed = await self._repository.delete(
                    connection,
                    workspace_id=scope.workspace_id,
                    user_id=user_id,
                )

                if removed is None:
                    raise ValidationError([MEMBER_NOT_FOUND])

                return removed

    async def list_invitations(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
    ) -> list[WorkspaceInvitationEntity]:
        """The workspace's outstanding invitations. Admins and owners only.

        Narrower than `list_members` deliberately: a pending invitation
        discloses an address that has not joined and may never, which is
        somebody else's email in a list an ordinary member has no reason to
        read.
        """
        require_workspace_admin(scope)

        async with self._pool.acquire() as connection:
            return await self._invitations.list_pending(
                connection,
                workspace_id=scope.workspace_id,
                limit=INVITATION_LIST_LIMIT,
            )

    async def create_invitation(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        email: str,
        role: str,
    ) -> tuple[WorkspaceInvitationEntity, str]:
        """Issue an invitation, and return the one copy of its raw token.

        The token is generated here, hashed, and only the hash is stored --
        so this return value is the single existence of the plaintext, and no
        query can produce it again. There is no mail delivery yet, so the
        inviter is handed the token to put in a link themselves; when mail
        lands, this return type is what stops being a pair.

        Never log it, never store it, and never put it in an error. The
        invitation entity carries neither it nor its digest, which is what
        keeps this the only frame that could.

        Only an owner may invite an owner, for the reason
        `update_member_role` gives: otherwise an admin mints an owner by
        inviting an address it controls.

        The address is stored as the inviter wrote it, per 004. Nothing looks
        an invitation up by email -- redemption is by token -- so normalising
        it would only make the list show something other than what was typed.
        """
        require_workspace_admin(scope)

        self._validate_invitation(email=email, role=role, scope=scope)

        token = generate_invitation_token()

        async with self._pool.acquire() as connection:
            invitation = await self._invitations.create(
                connection,
                workspace_id=scope.workspace_id,
                email=email,
                role=role,
                token_hash=hash_invitation_token(token),
                expires_in=INVITATION_LIFETIME,
            )

        return invitation, token

    async def revoke_invitation(
        self,
        *,
        scope: AuthorizedWorkspaceScope,
        invitation_id: UUID,
    ) -> UUID:
        """Withdraw an invitation before it is used.

        A single DELETE needs no explicit transaction. An id that names no
        invitation in this workspace and one that names none anywhere are the
        same answer, because the statement is scoped by workspace and cannot
        tell them apart.
        """
        require_workspace_admin(scope)

        async with self._pool.acquire() as connection:
            revoked = await self._invitations.delete(
                connection,
                workspace_id=scope.workspace_id,
                invitation_id=invitation_id,
            )

        if revoked is None:
            raise ValidationError([INVITATION_NOT_FOUND])

        return revoked

    async def accept_invitation(
        self,
        *,
        token: str,
        user_id: UUID,
    ) -> WorkspaceMembershipEntity:
        """Redeem an invitation into a membership, exactly once.

        The claim and the grant are one transaction, which is what makes a
        token single-use under concurrency. `claim` marks `accepted_at` under
        a row lock and hands back nothing to a second caller; the membership
        is inserted inside that same transaction, so a failed insert rolls the
        acceptance back and leaves the invitation usable rather than spending
        it on nothing.

        The role comes off the invitation row, never from the caller. Reading
        it from an argument would let whoever redeems a token choose what it
        was worth.

        `user_id` is the authenticated caller and not the address the
        invitation names. Matching the two would require this service to
        decide that an account owns an address, which is a claim only mail
        verification can make; the token is the credential, and holding it is
        the whole proof -- so an invitation forwarded to a colleague is
        accepted by the colleague, which is the ordinary case and not an
        attack.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                invitation = await self._invitations.claim(
                    connection,
                    token_hash=hash_invitation_token(token),
                )

                if invitation is None:
                    raise ValidationError([INVALID_INVITATION])

                try:
                    return await self._repository.create(
                        connection,
                        workspace_id=invitation.workspace_id,
                        user_id=user_id,
                        role=invitation.role,
                    )
                except asyncpg.UniqueViolationError as exc:
                    if exc.constraint_name != MEMBERSHIP_PKEY_CONSTRAINT:
                        raise

                    # Already a member. The rollback this raise causes takes
                    # `accepted_at` with it, so the invitation is left for
                    # whoever it was actually meant for.
                    raise ValidationError(
                        [
                            ValidationIssue(
                                field="token",
                                code="ALREADY_MEMBER",
                                message="You are already a member of this workspace",
                            )
                        ]
                    ) from None

    async def _lock_owners(
        self,
        connection: asyncpg.Connection,
        scope: AuthorizedWorkspaceScope,
    ) -> list[UUID]:
        return await self._repository.lock_owner_ids(
            connection,
            workspace_id=scope.workspace_id,
            owner_role=WORKSPACE_OWNER_ROLE,
        )

    @staticmethod
    def _require_owner_to_touch_owners(
        scope: AuthorizedWorkspaceScope,
        *,
        owners: list[UUID],
        user_id: UUID,
        role: str | None = None,
    ) -> None:
        """Only an owner may create an owner or act on one.

        Refused as WorkspaceAccessDeniedError rather than as a field error,
        because it is not a correctable input: the caller is an admin, and
        no different value in this request makes it allowed.
        """
        if scope.role == WORKSPACE_OWNER_ROLE:
            return

        if user_id in owners or role == WORKSPACE_OWNER_ROLE:
            raise WorkspaceAccessDeniedError()

    @staticmethod
    def _validate_workspace(*, name: str, slug: str) -> None:
        """Reject what the client can correct, before a connection is taken.

        The slug rules restate `workspaces_slug_format` because a CHECK
        violation reaches a client as a masked internal error, which a signup
        form cannot render and a person cannot act on. The constraint stays
        the backstop for every write that does not come through here.
        """
        issues = []

        if not name.strip():
            issues.append(
                ValidationIssue(
                    field="name",
                    code="REQUIRED",
                    message="Name is required",
                )
            )
        elif len(name.strip()) > NAME_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="name",
                    code="TOO_LONG",
                    message=f"Name must be at most {NAME_MAX_LENGTH} characters",
                )
            )

        if len(slug) > SLUG_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="slug",
                    code="TOO_LONG",
                    message=f"URL must be at most {SLUG_MAX_LENGTH} characters",
                )
            )
        elif not SLUG_PATTERN.match(slug):
            issues.append(
                ValidationIssue(
                    field="slug",
                    code="INVALID",
                    message=(
                        "URL may contain only lowercase letters, digits and "
                        "hyphens, and must start and end with one of the former"
                    ),
                )
            )

        if issues:
            raise ValidationError(issues)

    @staticmethod
    def _validate_invitation(
        *,
        email: str,
        role: str,
        scope: AuthorizedWorkspaceScope,
    ) -> None:
        if scope.role != WORKSPACE_OWNER_ROLE and role == WORKSPACE_OWNER_ROLE:
            raise WorkspaceAccessDeniedError()

        if len(email) > EMAIL_MAX_LENGTH or not EMAIL_PATTERN.match(email):
            raise ValidationError(
                [
                    ValidationIssue(
                        field="email",
                        code="INVALID",
                        message="Enter a valid email address",
                    )
                ]
            )
