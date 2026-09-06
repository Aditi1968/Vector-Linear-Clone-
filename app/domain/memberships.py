from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class WorkspaceMembershipEntity:
    """One `workspace_members` row, joined to the workspace it names.

    Flat rather than holding a nested workspace entity. Every read that
    produces one of these is a single join -- a membership is only ever
    interesting alongside the workspace it grants access to -- so the shape
    matches the row that comes back, and a caller cannot be handed a
    membership whose workspace half was fetched separately and may describe a
    different moment.

    `role` is a plain str holding one of app.domain.tenancy.WORKSPACE_ROLES;
    see the note there on why the domain does not model it as an enum.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    workspace_id: UUID
    workspace_slug: str
    workspace_name: str
    user_id: UUID
    role: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkspaceMemberEntity:
    """One `workspace_members` row, joined to the account it names.

    The mirror of WorkspaceMembershipEntity above: that one answers "which
    workspaces is this user in", this one answers "who is in this workspace".
    Same table, opposite direction, and the fields differ accordingly -- the
    workspace is already known to whoever asked, the person is not.

    Flat for the same reason, and it also keeps the account's address off a
    shared User type. `email` is here because a member list has to
    disambiguate two people with the same display name, and it is only ever
    produced for a caller whose membership of the workspace was checked first.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    user_id: UUID
    email: str
    name: str | None
    role: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkspaceInvitationEntity:
    """One `workspace_invitations` row, without the credential that redeems it.

    There is no `token` field and no `token_hash` field, and that omission is
    the point rather than an oversight. This is the type that travels out of
    the repository into services, resolvers and a GraphQL payload, so a token
    it carried would be one selected field away from a response body -- and an
    invitation token is a bearer credential for a whole tenant.

    The one raw token that ever exists is returned separately by
    `MembershipService.create_invitation`, travels one hop to the inviter, and
    is never stored. See migrations/004_membership.sql on `token_hash`.

    Pure application code -- no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    id: UUID
    workspace_id: UUID
    email: str
    role: str
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime
