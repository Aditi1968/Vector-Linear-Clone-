from datetime import datetime
from enum import Enum
from uuid import UUID

import strawberry

from app.domain.memberships import WorkspaceMemberEntity, WorkspaceMembershipEntity
from app.graphql.types.errors import ValidationErrorType


@strawberry.enum(name="WorkspaceRole")
class WorkspaceRoleType(Enum):
    """The transport's copy of app.domain.tenancy.WORKSPACE_ROLES.

    An enum here and a str in the domain, on purpose. A closed set is what a
    client wants -- generated types, exhaustive switches, no string literals
    in a UI -- while the value's authority comes from the CHECK constraint
    that admitted the row, not from anything this process declares.

    Members are declared least- to most-privileged, which is the order the
    SDL prints and therefore the order a generated client offers. It is
    documentation and not a comparison: GraphQL enums have no ordering.

    A role the database holds and this enum does not is a bug, and
    `WorkspaceMembershipType.from_entity` fails loudly on it rather than
    inventing a fallback -- see the note there. The three copies (constraint,
    domain tuple, this enum) are pinned equal by
    tests/test_membership_scope.py.
    """

    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"


@strawberry.type(name="Workspace")
class WorkspaceType:
    """The workspace a membership grants access to.

    Deliberately three fields. This type exists to name the workspace a
    membership points at, and every field on it is one the holder of that
    membership already knows: they typed the slug to get here. Anything a
    member should see but a stranger should not -- counts, settings, billing
    -- goes on a type reached through an authorized resolver, not onto this
    one, which is returned wherever a membership is.
    """

    id: UUID
    slug: str
    name: str

    @classmethod
    def from_membership(cls, entity: WorkspaceMembershipEntity) -> "WorkspaceType":
        """The workspace half of a membership row.

        A membership is where every workspace in this schema is read from --
        there is no unscoped workspace lookup, deliberately -- so this is the
        one way to build one, and it takes the entity that proves the caller
        may see it.
        """
        return cls(
            id=entity.workspace_id,
            slug=entity.workspace_slug,
            name=entity.workspace_name,
        )


@strawberry.type(name="WorkspaceMembership")
class WorkspaceMembershipType:
    """One workspace the viewer belongs to, and how.

    The workspace is nested rather than flattened into `workspaceId` and
    `workspaceSlug`, because the client's model of this is a workspace with a
    role attached -- a switcher renders the workspace and colours it by role.
    The domain entity is flat for the opposite and equally deliberate reason;
    see WorkspaceMembershipEntity.
    """

    workspace: WorkspaceType
    role: WorkspaceRoleType
    created_at: datetime

    @classmethod
    def from_entity(
        cls, entity: WorkspaceMembershipEntity
    ) -> "WorkspaceMembershipType":
        """Build the transport type, raising on a role this schema cannot say.

        `WorkspaceRoleType(entity.role)` raises ValueError for a role the
        enum does not carry, which reaches the client as a masked internal
        error and reaches the logs as a real traceback. That is the intended
        outcome and the alternatives are worse: a fallback member would tell
        a client someone holds the least privileged role when the database
        says otherwise, and an Optional role would push the same guess into
        every consumer. The condition is unreachable while the constraint,
        WORKSPACE_ROLES and this enum agree, and a test fails first if they
        stop agreeing.
        """
        return cls(
            workspace=WorkspaceType.from_membership(entity),
            role=WorkspaceRoleType(entity.role),
            created_at=entity.created_at,
        )


@strawberry.type(
    name="WorkspaceMember",
    description="One person in a workspace, and the role they hold there.",
)
class WorkspaceMemberType:
    """A member as an assignee picker and a members settings page read one.

    Flat, and not a `User` with a role beside it. `User` is the type `me`
    returns, and its own docstring records the condition this would have
    broken: exposing an address there is safe only while the only address any
    caller can read is their own. A member list is exactly the moment that
    stops being true, so the fields a member may see about a colleague are
    listed here rather than borrowed from a type that will grow fields --
    a session count, a last-seen -- nobody meant to publish workspace-wide.

    Reachable only through `workspaceMembers`, which resolves an
    AuthorizedWorkspaceScope first. There is no path to one of these from an
    issue or a project, so an address cannot be read by a caller whose
    membership was never checked.
    """

    user_id: UUID
    email: str
    name: str | None
    role: WorkspaceRoleType
    created_at: datetime

    removed_at: datetime | None = strawberry.field(
        description=(
            "When this person left the workspace, or null if they are still "
            "in it. A member list includes people who have left, so that "
            "anything they wrote still renders with their name; anything "
            "offering a choice of person -- an assignee picker, a lead -- "
            "must exclude the ones this field is set on."
        )
    )

    @classmethod
    def from_entity(cls, entity: WorkspaceMemberEntity) -> "WorkspaceMemberType":
        """Build the transport type, raising on a role this schema cannot say.

        Same contract as `WorkspaceMembershipType.from_entity`; see the note
        there on why an unknown role fails loudly rather than falling back.

        `removed_at` is on the wire rather than filtered out behind it, and the
        client-side difference is real: without it, a comment by somebody who
        has left resolves to no entry in the member map, which is
        indistinguishable from a lookup that simply failed -- so the two render
        the same and one of them is a bug nobody sees. With it, "this person
        left" is a value the row carries and "this person is missing" stays an
        error worth noticing.

        `role` is still reported for a former member. It is what they held when
        they left and it is what a history screen needs to render the row; it
        grants nothing, because nothing decides permission from this type.
        """
        return cls(
            user_id=entity.user_id,
            email=entity.email,
            name=entity.name,
            role=WorkspaceRoleType(entity.role),
            created_at=entity.created_at,
            removed_at=entity.removed_at,
        )


@strawberry.type
class WorkspacePayload:
    """The result of creating a workspace.

    Carries the workspace rather than the membership that came with it. The
    membership is what was written -- see
    `MembershipService.create_workspace` -- but what the client does next is
    navigate to the workspace it just made, and it already knows its own role
    is owner because it is the account that created it.
    """

    workspace: WorkspaceType | None
    errors: list[ValidationErrorType]


@strawberry.type
class WorkspaceMemberPayload:
    member: WorkspaceMemberType | None
    errors: list[ValidationErrorType]


@strawberry.type
class MemberRemovePayload:
    """The result of ending a membership.

    The id rather than the member. The row is not gone -- 026 keeps it so that
    everything the person wrote still resolves to them -- but nothing about it
    that a client is holding has changed except `removedAt`, and the id is what
    a cache needs to go and re-read the one entry that moved.
    """

    removed_user_id: UUID | None
    errors: list[ValidationErrorType]
