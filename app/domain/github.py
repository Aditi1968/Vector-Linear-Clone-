from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID


# What a workspace's GitHub integration can be, and the three answers are
# deliberately three rather than a boolean.
#
# UNCONFIGURED  this deployment has no GitHub App credentials at all, so no
#               workspace here can connect one. An operator's problem.
# DISCONNECTED  the deployment is configured; this workspace has not
#               installed the app, or has removed it. A user's problem, and
#               one a "Connect" button fixes.
# CONNECTED     an installation row exists for this workspace.
#
# Collapsing the first two into "not connected" is the mistake this tuple
# exists to prevent: it would put a Connect button in front of a user whose
# click cannot possibly work, and hide from the operator that the deployment
# they are looking at was never given a key.
#
# Order is documentation only; nothing may read a comparison out of it. The
# GraphQL enum in app/graphql/types/github.py is the transport's copy, and
# tests/test_github.py pins the two equal.
GITHUB_STATUSES: Final = ("unconfigured", "disconnected", "connected")

UNCONFIGURED: Final = "unconfigured"
DISCONNECTED: Final = "disconnected"
CONNECTED: Final = "connected"


@dataclass(frozen=True, slots=True)
class GithubInstallationEntity:
    """One workspace's installation of the GitHub App.

    Carries no `workspace_id`, exactly as CycleEntity carries no workspace:
    it is only ever read through a scope the caller already holds, and a
    second copy travelling on the entity is the copy that eventually gets
    trusted by mistake.

    Carries no token, no key and no signing material, and never will. The
    row it is built from has no such column; see
    migrations/013_github_integration.sql.

    Pure application code: no Strawberry, FastAPI, asyncpg or PostgreSQL.
    """

    installation_id: int

    # None until the first signed `installation` webhook tells us the account.
    # A real, short-lived state rather than a missing value -- the setup
    # redirect GitHub sends the admin back with does not carry it.
    account_login: str | None

    connected_by: UUID
    connected_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class GithubRepositoryEntity:
    """One repository an installation covers.

    `repository_id` is GitHub's, and is what survives a rename; `full_name`
    is what a human reads and what a rename rewrites.
    """

    repository_id: int
    full_name: str


@dataclass(frozen=True, slots=True)
class GithubIntegrationEntity:
    """What one workspace's integration looks like right now.

    `installation` is present whenever a row exists, including when `status`
    is UNCONFIGURED -- a deployment whose credentials were removed still has
    workspaces that connected under the old ones, and hiding that would tell
    a user their integration is gone when it is only unusable.
    """

    status: str
    installation: GithubInstallationEntity | None
    repositories: tuple[GithubRepositoryEntity, ...]
