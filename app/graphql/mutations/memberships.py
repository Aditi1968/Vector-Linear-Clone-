import strawberry
from strawberry.types import Info

from app.domain.errors import (
    TeamNotFoundError,
    ValidationError,
    WorkspaceAccessDeniedError,
)
from app.domain.tenancy import AuthorizedWorkspaceScope
from app.graphql.inputs.membership import (
    InvitationAcceptInput,
    InvitationCreateInput,
    InvitationRevokeInput,
    MemberRemoveInput,
    MemberRoleUpdateInput,
    TeamCreateInput,
    TeamEstimateScaleSetInput,
    WorkspaceCreateInput,
)
from app.graphql.queries.memberships import workspace_not_found
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.invitation import (
    InvitationAcceptPayload,
    InvitationCreatePayload,
    InvitationRevokePayload,
    WorkspaceInvitationType,
)
from app.graphql.types.membership import (
    MemberRemovePayload,
    WorkspaceMemberPayload,
    WorkspaceMemberType,
    WorkspacePayload,
    WorkspaceType,
)
from app.graphql.types.team import TeamPayload, TeamType
from app.graphql.viewer import viewer_user_id


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    """The domain's structured issues, as the transport reports them."""
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class MembershipMutation:
    """Onboarding and membership: making a workspace, and choosing who is in it.

    Every resolver here is the same shape. Identify the viewer, resolve the
    workspace slug against their memberships, call the service, and translate
    exactly two exceptions -- a refusal into one NOT_FOUND error, a validation
    failure into `payload.errors`. Nothing here decides anything: which roles
    may invite, whether a workspace still has an owner, and whether a token is
    still good are rules the service owns, and a resolver that re-checked any
    of them would be a second copy that can disagree with the first.

    The refusal is deliberately raised rather than returned in `errors`. It is
    not a correctable input -- no different value in the request makes a
    non-member an admin -- and it must read identically whether the workspace
    is missing, invisible or merely not the caller's to administer.
    """

    @strawberry.mutation(
        description=("Create a workspace. The authenticated caller becomes its owner.")
    )
    async def workspace_create(
        self, info: Info, input: WorkspaceCreateInput
    ) -> WorkspacePayload:
        """Create a workspace and the caller's ownership of it, together.

        The first mutation of onboarding, and the only workspace-scoped one
        here that resolves no slug: there is no workspace to be a member of
        yet, so an authenticated identity is the whole of the check.
        """
        user_id = await viewer_user_id(info)

        try:
            membership = await info.context.membership_service.create_workspace(
                name=input.name,
                slug=input.slug,
                owner_id=user_id,
            )
        except ValidationError as exc:
            # Only expected input validation is translated into the payload.
            # Everything else (asyncpg failures, bugs, outages) propagates
            # through GraphQL's normal error mechanism and is masked there.
            return WorkspacePayload(workspace=None, errors=_errors(exc))

        return WorkspacePayload(
            workspace=WorkspaceType.from_membership(membership),
            errors=[],
        )

    @strawberry.mutation(
        description=(
            "Create a team, seeded with the default workflow states. Requires "
            "the admin or owner role."
        )
    )
    async def team_create(self, info: Info, input: TeamCreateInput) -> TeamPayload:
        """Create a team with a usable board.

        The board is not optional and not a second call: `TeamService.create`
        writes both in one transaction, because a team without workflow states
        accepts no issues at all.
        """
        try:
            scope = await _scope(info, input.workspace_slug)

            workflow = await info.context.team_service.create(
                scope=scope,
                name=input.name,
                key=input.key,
            )
        except WorkspaceAccessDeniedError:
            raise workspace_not_found() from None
        except ValidationError as exc:
            return TeamPayload(team=None, errors=_errors(exc))

        return TeamPayload(team=TeamType.from_domain(workflow), errors=[])

    @strawberry.mutation(
        description=(
            "Choose what a team's estimates count -- points, hours, t-shirt "
            "sizes, or nothing named. Requires the admin or owner role. Issues "
            "already estimated keep their numbers; the scale bounds what may "
            "be written from now on."
        )
    )
    async def team_estimate_scale_set(
        self, info: Info, input: TeamEstimateScaleSetInput
    ) -> TeamPayload:
        """Set the unit a team estimates in.

        Declared here beside `team_create` rather than in a mutation class of
        its own, because it is the second thing anybody does to a team and a
        one-field feature class merged into the root would be a file, an import
        and an entry in `MUTATION_TYPES` bought for one resolver. If teams grow
        a third mutation, they earn their own module and both move together.

        `TeamPayload`, the same type `team_create` answers, and the service
        reads the board back inside its transaction so it is populated. A
        payload whose `workflowStates` were empty would be a client's cue to
        blank the board it is rendering.

        Requires admin or owner, unlike labels, cycles and templates, which any
        member may write: this setting decides which estimates the WHOLE team
        may write from now on, so a member switching it would start refusing
        their colleagues' next edit.
        """
        try:
            scope = await _scope(info, input.workspace_slug)

            workflow = await info.context.team_service.set_estimate_scale(
                scope=scope,
                team_id=input.team_id,
                # The enum member and not `.value`: `TeamService` takes an
                # `EstimateScale`, so the vocabulary is checked once by GraphQL
                # validation and never converted back to a bare string on the
                # way in. `member_role_update` beside this passes `.value`
                # because roles are a plain `str` in the domain, deliberately
                # -- see `AuthorizedWorkspaceScope.role`.
                scale=input.scale,
            )
        except WorkspaceAccessDeniedError:
            raise workspace_not_found() from None
        except TeamNotFoundError:
            # A team in another workspace and one that does not exist are the
            # same field error, because telling them apart would confirm that
            # another tenant's team is real.
            return TeamPayload(
                team=None,
                errors=[
                    ValidationErrorType(
                        field="teamId",
                        code="NOT_FOUND",
                        message="Team not found",
                    )
                ],
            )
        except ValidationError as exc:
            return TeamPayload(team=None, errors=_errors(exc))

        return TeamPayload(team=TeamType.from_domain(workflow), errors=[])

    @strawberry.mutation(
        description="Change a member's role. Requires the admin or owner role."
    )
    async def member_role_update(
        self, info: Info, input: MemberRoleUpdateInput
    ) -> WorkspaceMemberPayload:
        """Set one member's role.

        `input.role` is the GraphQL enum; `.value` is the string the database
        stores and the domain compares. The conversion happens here, at the
        transport boundary, which is the same place the reverse conversion
        happens on the way out.
        """
        try:
            scope = await _scope(info, input.workspace_slug)

            member = await info.context.membership_service.update_member_role(
                scope=scope,
                user_id=input.user_id,
                role=input.role.value,
            )
        except WorkspaceAccessDeniedError:
            raise workspace_not_found() from None
        except ValidationError as exc:
            return WorkspaceMemberPayload(member=None, errors=_errors(exc))

        return WorkspaceMemberPayload(
            member=WorkspaceMemberType.from_entity(member),
            errors=[],
        )

    @strawberry.mutation(
        description=(
            "Remove someone from the workspace. Requires the admin or owner "
            "role, and cannot remove the last owner."
        )
    )
    async def member_remove(
        self, info: Info, input: MemberRemoveInput
    ) -> MemberRemovePayload:
        try:
            scope = await _scope(info, input.workspace_slug)

            removed = await info.context.membership_service.remove_member(
                scope=scope,
                user_id=input.user_id,
            )
        except WorkspaceAccessDeniedError:
            raise workspace_not_found() from None
        except ValidationError as exc:
            return MemberRemovePayload(removed_user_id=None, errors=_errors(exc))

        return MemberRemovePayload(removed_user_id=removed, errors=[])

    @strawberry.mutation(
        description=(
            "Invite an email address to the workspace. Returns the raw "
            "invitation token once and never again."
        )
    )
    async def invitation_create(
        self, info: Info, input: InvitationCreateInput
    ) -> InvitationCreatePayload:
        """Issue an invitation and hand the inviter its token.

        There is no mail delivery yet, so this payload is how the link reaches
        the person invited: the inviter copies the token out of it and sends
        it themselves. Only the sha256 digest is stored, so this response is
        the single existence of the plaintext -- it cannot be fetched again,
        and a client that loses it must issue a new invitation.
        """
        try:
            scope = await _scope(info, input.workspace_slug)

            invitation, token = await info.context.membership_service.create_invitation(
                scope=scope,
                email=input.email,
                role=input.role.value,
            )
        except WorkspaceAccessDeniedError:
            raise workspace_not_found() from None
        except ValidationError as exc:
            return InvitationCreatePayload(
                invitation=None,
                token=None,
                errors=_errors(exc),
            )

        return InvitationCreatePayload(
            invitation=WorkspaceInvitationType.from_entity(invitation),
            token=token,
            errors=[],
        )

    @strawberry.mutation(
        description="Withdraw an invitation. Requires the admin or owner role."
    )
    async def invitation_revoke(
        self, info: Info, input: InvitationRevokeInput
    ) -> InvitationRevokePayload:
        try:
            scope = await _scope(info, input.workspace_slug)

            revoked = await info.context.membership_service.revoke_invitation(
                scope=scope,
                invitation_id=input.id,
            )
        except WorkspaceAccessDeniedError:
            raise workspace_not_found() from None
        except ValidationError as exc:
            return InvitationRevokePayload(
                revoked_invitation_id=None,
                errors=_errors(exc),
            )

        return InvitationRevokePayload(revoked_invitation_id=revoked, errors=[])

    @strawberry.mutation(
        description="Redeem an invitation token as the authenticated caller."
    )
    async def invitation_accept(
        self, info: Info, input: InvitationAcceptInput
    ) -> InvitationAcceptPayload:
        """Join the workspace an invitation names.

        No workspace slug is resolved and no role is checked, because the
        token is the authorization: the caller is by definition not yet a
        member. What is required is an identity, since the whole operation is
        to attach a workspace to an account.

        A token that is unknown, revoked, expired or already accepted is one
        answer, in `errors`. Distinguishing them would let a caller ask
        whether a string was ever a real invitation here.
        """
        user_id = await viewer_user_id(info)

        try:
            membership = await info.context.membership_service.accept_invitation(
                token=input.token,
                user_id=user_id,
            )
        except ValidationError as exc:
            return InvitationAcceptPayload(workspace=None, errors=_errors(exc))

        return InvitationAcceptPayload(
            workspace=WorkspaceType.from_membership(membership),
            errors=[],
        )


async def _scope(info: Info, workspace_slug: str) -> AuthorizedWorkspaceScope:
    """Identify the viewer, then resolve the slug against their memberships.

    Identity first and unconditionally, so an unauthenticated request performs
    no workspace lookup at all -- see `viewer_user_id`.

    Raises WorkspaceAccessDeniedError rather than translating it, unlike its
    twin in `app/graphql/queries/memberships.py`: every caller here already
    has a `try` around the service call, and the service raises the same
    exception for an insufficient role. One `except` covering both is what
    keeps "not a member" and "not an admin" from being two different
    responses.

    A stand-in until `app/graphql/scope.py` lands, at which point this becomes
    a call to the one helper every workspace-scoped field shares.
    """
    user_id = await viewer_user_id(info)

    # Annotated rather than returned inline: `info.context` is untyped here, so
    # returning the call directly would satisfy any return type -- including a
    # bare WorkspaceScope, which carries no evidence that anything was checked.
    scope: AuthorizedWorkspaceScope = (
        await info.context.membership_service.authorized_scope_for_slug(
            slug=workspace_slug,
            user_id=user_id,
        )
    )

    return scope
