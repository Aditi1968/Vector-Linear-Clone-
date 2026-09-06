from uuid import UUID

import strawberry

from app.graphql.types.membership import WorkspaceRoleType


@strawberry.input
class WorkspaceCreateInput:
    """A new workspace, named by the account creating it.

    No `ownerId`. The owner is the authenticated caller and nothing else --
    a field here would be an API for granting someone else a tenant they
    never asked for, and for the caller to disclaim what it just created.
    """

    name: str
    slug: str = strawberry.field(
        description=(
            "The workspace's URL segment. Lowercase letters, digits and "
            "hyphens; must start and end with a letter or digit."
        )
    )


@strawberry.input
class TeamCreateInput:
    """A new team in an existing workspace.

    The workspace is named by slug, never by id: the server resolves it and
    checks the caller's role against the row it found. A workspace id
    supplied by a client is a value, not an authorisation.
    """

    workspace_slug: str
    name: str
    key: str = strawberry.field(
        description=(
            "The prefix of this team's issue identifiers -- the ENG in "
            "ENG-42. 1-10 uppercase letters and digits, starting with a "
            "letter. Unique within the workspace."
        )
    )


@strawberry.input
class MemberRoleUpdateInput:
    """A member's new role.

    `role` is the enum rather than a String, so an unknown value is refused
    during GraphQL validation -- before a resolver runs, before a connection
    is taken -- instead of reaching the CHECK constraint as a masked error.
    """

    workspace_slug: str
    user_id: UUID
    role: WorkspaceRoleType


@strawberry.input
class MemberRemoveInput:
    workspace_slug: str
    user_id: UUID


@strawberry.input
class InvitationCreateInput:
    workspace_slug: str
    email: str
    role: WorkspaceRoleType


@strawberry.input
class InvitationRevokeInput:
    workspace_slug: str
    id: UUID


@strawberry.input
class InvitationAcceptInput:
    """The token from an invitation link, and nothing else.

    No workspace slug: the token already names the workspace, and accepting
    one that named a different workspace than the token does would be a
    question the server has no reason to ask. Who is joining is the
    authenticated caller.
    """

    token: str
