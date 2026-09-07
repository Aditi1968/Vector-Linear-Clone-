from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID


# What a workspace's GitHub integration can be, and the four answers are
# deliberately four rather than a boolean.
#
# UNCONFIGURED  this deployment has no GitHub App credentials at all, so no
#               workspace here can connect one. An operator's problem.
# DISCONNECTED  the deployment is configured; this workspace has not
#               installed the app, or has removed it. A user's problem, and
#               one a "Connect" button fixes.
# PENDING       this workspace has claimed an installation and GitHub has not
#               confirmed it. Nobody's problem yet, and nobody's data either:
#               the account and the repositories stay empty until a signed
#               delivery says the claim was true.
# CONNECTED     GitHub has confirmed the claim.
#
# Collapsing UNCONFIGURED and DISCONNECTED into "not connected" would put a
# Connect button in front of a user whose click cannot possibly work, and hide
# from the operator that the deployment they are looking at was never given a
# key. Collapsing PENDING into CONNECTED is worse, and is the defect
# migrations/016_github_installation_trust.sql exists to close: an id the
# caller typed is not evidence that the caller owns the installation it names,
# so reporting a claim as a connection reports somebody else's organisation as
# this workspace's.
#
# Order is documentation only; nothing may read a comparison out of it. The
# GraphQL enum in app/graphql/types/github.py is the transport's copy, and
# tests/test_github.py pins the two equal.
GITHUB_STATUSES: Final = ("unconfigured", "disconnected", "pending", "connected")

UNCONFIGURED: Final = "unconfigured"
DISCONNECTED: Final = "disconnected"
PENDING: Final = "pending"
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
    # redirect GitHub sends the admin back with does not carry it. While
    # `confirmed_at` is None this is None too, and the database says so rather
    # than the writer remembering to: see
    # github_installations_unconfirmed_holds_no_account in 016.
    account_login: str | None

    connected_by: UUID
    connected_at: datetime
    updated_at: datetime

    # When a signature-verified delivery named this installation, or None while
    # the row is still only a claim the callback wrote.
    #
    # This is the whole difference between "a workspace said it installed the
    # app" and "GitHub said so", and every read that matters keys off it: an
    # unconfirmed row is PENDING, resolves no webhook, and may hold nothing
    # about the organisation it names.
    confirmed_at: datetime | None


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
