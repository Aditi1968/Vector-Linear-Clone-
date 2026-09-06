from datetime import datetime
from enum import Enum
from uuid import UUID

import strawberry

from app.domain.github import GithubIntegrationEntity


@strawberry.enum(name="GithubIntegrationStatus")
class GithubIntegrationStatusType(Enum):
    """The transport's copy of app.domain.github.GITHUB_STATUSES.

    Three members rather than a `connected: Boolean`, because a client has to
    render three different things: an operator's notice for a deployment with
    no GitHub App, a Connect button for a workspace that has not installed it,
    and the installation itself. A boolean would make the first two identical
    and put a button in front of a user whose click cannot work.

    tests/test_github.py pins these members equal to the domain tuple.
    """

    UNCONFIGURED = "unconfigured"
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"


@strawberry.type(name="GithubRepository")
class GithubRepositoryType:
    """One repository the installation covers.

    `repositoryId` is an ID rather than an Int deliberately. GraphQL's Int is
    signed 32-bit, and GitHub's repository ids are an opaque counter it owns
    and grows -- a value past 2^31 would be a serialisation error on a field
    that had worked for years. ID serialises as a string and carries no
    arithmetic promise, which is the honest description of an identifier from
    somebody else's system.
    """

    repository_id: strawberry.ID
    full_name: str


@strawberry.type(name="GithubIntegration")
class GithubIntegrationType:
    """A workspace's GitHub integration, as an admin of it sees it.

    Every field here is a fact about *what* is connected. None of them is a
    credential, and none can become one by accident: there is no token field,
    no secret field, no signing material, and the entity behind this type is
    built from a table that has no such column -- so adding one would take a
    migration, a repository change and a resolver change rather than a typo.

    `accountLogin` is nullable because the account is not known at the instant
    the installation is recorded; the setup redirect does not carry it, and
    the first signed webhook does. `connectedAt` and `connectedById` are
    nullable for the plainer reason that there may be no installation at all.
    """

    status: GithubIntegrationStatusType
    account_login: str | None
    connected_at: datetime | None
    connected_by_id: UUID | None
    repositories: list[GithubRepositoryType]

    @classmethod
    def from_entity(cls, entity: GithubIntegrationEntity) -> "GithubIntegrationType":
        """Build the transport type, raising on a status this schema cannot say.

        `GithubIntegrationStatusType(entity.status)` raises ValueError for a
        status the enum does not carry, which reaches the client as a masked
        internal error and the logs as a real traceback. That is the intended
        outcome, for the reason WorkspaceMembershipType.from_entity gives: a
        fallback would report a state the server does not believe.
        """
        installation = entity.installation

        return cls(
            status=GithubIntegrationStatusType(entity.status),
            account_login=None if installation is None else installation.account_login,
            connected_at=None if installation is None else installation.connected_at,
            connected_by_id=(
                None if installation is None else installation.connected_by
            ),
            repositories=[
                GithubRepositoryType(
                    repository_id=strawberry.ID(str(repository.repository_id)),
                    full_name=repository.full_name,
                )
                for repository in entity.repositories
            ],
        )
