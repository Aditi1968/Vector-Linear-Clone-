from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.memberships import WorkspaceInvitationEntity
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.membership import WorkspaceRoleType, WorkspaceType


@strawberry.type(
    name="WorkspaceInvitation",
    description="An outstanding invitation to join a workspace.",
)
class WorkspaceInvitationType:
    """An invitation as its workspace's admins see one.

    There is no `token` field and no `tokenHash` field. That is not an
    omission to be filled in later: the token is a bearer credential for the
    whole tenant, so publishing it here would let anyone who can list
    invitations join as anyone who was invited, and publishing the digest
    would hand an offline attacker the value redemption compares against.
    `WorkspaceInvitationEntity` carries neither, so neither can be added here
    by accident.

    `workspaceId` is absent for the reason spelled out at the foot of
    app/graphql/types/team.py: a client addresses a workspace by slug, and the
    server must never accept a workspace id it handed out.
    """

    id: UUID
    email: str
    role: WorkspaceRoleType
    expires_at: datetime
    created_at: datetime

    @classmethod
    def from_entity(
        cls, entity: WorkspaceInvitationEntity
    ) -> "WorkspaceInvitationType":
        return cls(
            id=entity.id,
            email=entity.email,
            role=WorkspaceRoleType(entity.role),
            expires_at=entity.expires_at,
            created_at=entity.created_at,
        )


@strawberry.type
class InvitationCreatePayload:
    """A new invitation, and the only copy of its token that will ever exist.

    `token` is returned exactly once, from this mutation, and can never be
    read again: only its sha256 digest is stored, so no query can produce it
    and no later field exposes it.

    It is here because there is no mail delivery yet. The inviter is the one
    who has to get the link to the person invited, so the server hands them
    the token to build it with. When mail lands, this field goes away and the
    token stops leaving the server at all -- so a client should treat it as
    something to show once and not as something to keep.
    """

    invitation: WorkspaceInvitationType | None
    token: str | None
    errors: list[ValidationErrorType]


@strawberry.type
class InvitationRevokePayload:
    """The id rather than the invitation: the row is gone."""

    revoked_invitation_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class InvitationAcceptPayload:
    """The workspace the caller has just joined.

    Enough for a client to navigate straight into it, which is the whole
    reason someone followed an invitation link. The role they joined as is on
    `myWorkspaces`, which a client refetches anyway to update its switcher.
    """

    workspace: WorkspaceType | None
    errors: list[ValidationErrorType]
