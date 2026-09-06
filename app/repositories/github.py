from collections.abc import Sequence
from uuid import UUID

import asyncpg

from app.domain.errors import GithubInstallationClaimedError
from app.domain.github import GithubInstallationEntity, GithubRepositoryEntity
from app.domain.tenancy import WorkspaceScope


# Compared by name because a UniqueViolationError alone does not say which
# constraint it came from, and this table has more than one way to collide:
# the workspace primary key means "this workspace is already connected", which
# is the caller's own ordering mistake, while this one means another tenant got
# there first, which is not.
INSTALLATION_ID_UNIQUE_CONSTRAINT = "github_installations_installation_id_key"


class GithubRepository:
    """SQL access for `github_installations` and `github_repositories`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement but one is scoped to a workspace, and the scope arrives as
    a required keyword argument. The exception is
    `find_workspace_by_installation_id`, which is how a webhook *acquires* a
    workspace and therefore cannot already have one; it is the only door into
    this data that a scope does not guard, and its docstring says what stands
    in for one.

    Nothing here selects a token, a key or a signature, because no such column
    exists -- see migrations/013_github_integration.sql. That is worth stating
    in the layer that writes the SELECT lists: a column added later would be
    one `SELECT *` away from the GraphQL layer, and there is no `SELECT *`
    here either.
    """

    async def get_installation(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> GithubInstallationEntity | None:
        """This workspace's installation, or nothing.

        The workspace is the primary key, so this is a single-row lookup and
        another tenant's installation is not something the statement can
        return -- there is no id argument for a caller to aim elsewhere.
        """
        row = await connection.fetchrow(
            """
            SELECT
                installation_id,
                account_login,
                connected_by,
                connected_at,
                updated_at
            FROM github_installations
            WHERE workspace_id = $1
            """,
            scope.workspace_id,
        )

        if row is None:
            return None

        return self._to_installation(row)

    async def list_repositories(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> list[GithubRepositoryEntity]:
        """Every repository this workspace's installation covers, by name.

        Unpaginated on purpose: an installation covers the repositories an
        admin ticked, which is tens at most and is rendered as one list on one
        settings page. `github_repositories_pkey` serves the predicate and the
        sort is over what it returns.
        """
        rows = await connection.fetch(
            """
            SELECT repository_id, full_name
            FROM github_repositories
            WHERE workspace_id = $1
            ORDER BY full_name
            """,
            scope.workspace_id,
        )

        return [self._to_repository(row) for row in rows]

    async def insert_installation(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        installation_id: int,
        connected_by: UUID,
    ) -> GithubInstallationEntity:
        """Record that this workspace has installed the app.

        No ON CONFLICT. Re-connecting is a delete followed by an insert, run
        by the service inside one transaction, for the reason
        `IssueLabelRepository.attach` gives: an upsert reports success without
        establishing that the row it found is the row this call meant to
        write, and here that row could be an *older account's* installation
        whose repositories are still sitting in the child table.

        An `installation_id` already claimed by another workspace raises
        GithubInstallationClaimedError, translated from the constraint here
        rather than upstream: reading a constraint name is SQL knowledge, and
        a bare UniqueViolationError says nothing on its own -- it could be any
        constraint on any table. The constraint is what decides, because a
        SELECT first is a decision another transaction could invalidate before
        the INSERT landed.
        """
        try:
            row = await connection.fetchrow(
                """
                INSERT INTO github_installations (
                    workspace_id,
                    installation_id,
                    connected_by
                )
                VALUES ($1, $2, $3)
                RETURNING
                    installation_id,
                    account_login,
                    connected_by,
                    connected_at,
                    updated_at
                """,
                scope.workspace_id,
                installation_id,
                connected_by,
            )
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != INSTALLATION_ID_UNIQUE_CONSTRAINT:
                raise

            raise GithubInstallationClaimedError() from None

        return self._to_installation(row)

    async def delete_installation(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> bool:
        """Remove this workspace's installation; whether there was one.

        The repositories must already be gone -- `github_repositories_
        installation_fk` is RESTRICT -- which is the ordering
        `GithubService.disconnect` performs in its transaction.

        RETURNING rather than the command tag: the tag arrives as text that
        has to be parsed, and a row either came back or did not.
        """
        row = await connection.fetchrow(
            """
            DELETE FROM github_installations
            WHERE workspace_id = $1
            RETURNING workspace_id
            """,
            scope.workspace_id,
        )

        return row is not None

    async def set_account_login(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        account_login: str,
    ) -> None:
        """Record the account GitHub says this installation lives in.

        Written from a webhook payload whose signature has already been
        verified, and shape-checked again by
        `github_installations_account_login_format` on the way in -- so a
        login that is not one is an error from this statement rather than a
        value in the table.
        """
        await connection.execute(
            """
            UPDATE github_installations
            SET account_login = $2, updated_at = now()
            WHERE workspace_id = $1
            """,
            scope.workspace_id,
            account_login,
        )

    async def find_workspace_by_installation_id(
        self,
        connection: asyncpg.Connection,
        *,
        installation_id: int,
    ) -> UUID | None:
        """Which workspace owns this installation, if any.

        The one statement here that is not scoped, because it is what
        *produces* a scope: a webhook delivery names an installation and
        nothing else, so there is no workspace to pass in until this has run.

        What stands in for a scope is two things. The caller has already
        verified GitHub's HMAC over the raw body, so the installation id is
        one GitHub sent rather than one a client chose; and
        `github_installations_installation_id_key` makes the answer at most
        one workspace, so a delivery can never fan out across tenants.

        None means no workspace here has connected that installation, which is
        the ordinary case for an app installed into an account that then
        never completed the callback. The caller drops the delivery.
        """
        # Annotated rather than returned inline: asyncpg is untyped, so
        # returning the call directly would satisfy any return type at all.
        workspace_id: UUID | None = await connection.fetchval(
            """
            SELECT workspace_id
            FROM github_installations
            WHERE installation_id = $1
            """,
            installation_id,
        )

        return workspace_id

    async def add_repositories(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repositories: Sequence[GithubRepositoryEntity],
    ) -> None:
        """Add these repositories to this workspace's installation.

        One statement for the whole set rather than one per repository: an
        installation covering fifty repositories would otherwise be fifty
        round trips inside the webhook handler's transaction.

        No ON CONFLICT, and therefore a primary key violation if a repository
        is already there. That is the caller's job to avoid, and
        `GithubService` does it by deleting the ids it is about to write in
        the same transaction -- which is also what makes a rename land, since
        the delete-and-insert replaces `full_name` rather than merging it.
        """
        if not repositories:
            return

        await connection.execute(
            """
            INSERT INTO github_repositories (
                workspace_id,
                repository_id,
                full_name
            )
            SELECT $1, incoming.repository_id, incoming.full_name
            FROM unnest($2::BIGINT[], $3::TEXT[])
                AS incoming(repository_id, full_name)
            """,
            scope.workspace_id,
            [repository.repository_id for repository in repositories],
            [repository.full_name for repository in repositories],
        )

    async def delete_repositories(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_ids: Sequence[int] | None = None,
    ) -> None:
        """Remove repositories from this workspace's installation.

        `repository_ids=None` means all of them, which is what disconnecting
        needs and what replacing the whole set needs. A list means exactly
        those, which is what `installation_repositories.removed` needs. One
        statement covers both because the alternative is two methods whose SQL
        differs by a single predicate, and two places for the tenant filter to
        be forgotten in.

        The array is cast on the server, so `NULL::BIGINT[]` is what an
        omitted list becomes and the predicate short-circuits to "every row of
        this workspace".
        """
        await connection.execute(
            """
            DELETE FROM github_repositories
            WHERE workspace_id = $1
              AND ($2::BIGINT[] IS NULL OR repository_id = ANY($2::BIGINT[]))
            """,
            scope.workspace_id,
            None if repository_ids is None else list(repository_ids),
        )

    @staticmethod
    def _to_installation(row: asyncpg.Record) -> GithubInstallationEntity:
        return GithubInstallationEntity(
            installation_id=row["installation_id"],
            account_login=row["account_login"],
            connected_by=row["connected_by"],
            connected_at=row["connected_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _to_repository(row: asyncpg.Record) -> GithubRepositoryEntity:
        return GithubRepositoryEntity(
            repository_id=row["repository_id"],
            full_name=row["full_name"],
        )
