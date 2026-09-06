from datetime import datetime
from enum import Enum
from uuid import UUID

import strawberry

from app.domain.memberships import WorkspaceMembershipEntity


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
            workspace=WorkspaceType(
                id=entity.workspace_id,
                slug=entity.workspace_slug,
                name=entity.workspace_name,
            ),
            role=WorkspaceRoleType(entity.role),
            created_at=entity.created_at,
        )
