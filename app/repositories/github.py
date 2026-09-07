from collections.abc import Sequence
from datetime import timedelta
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

    Most statements are scoped to a workspace, and the scope arrives as a
    required keyword argument. Three are not --
    `find_confirmed_workspace_by_installation_id`, `confirm_installation` and
    `delete_expired_claim` -- because each is on the path by which a webhook or
    a claim *acquires* a workspace and therefore cannot already have one. They
    are the only doors into this data that a scope does not guard, and each
    docstring says what stands in for one.

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
                updated_at,
                confirmed_at
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
        """Record that this workspace CLAIMS to have installed the app.

        `confirmed_at` is left NULL and is not a parameter, which is the point
        of the whole exercise: the installation id arrived in a query string,
        so this statement records who claimed what and nothing more. Only
        `confirm_installation` below -- reached from a delivery whose HMAC has
        been verified -- turns the claim into a connection.

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
                    updated_at,
                    confirmed_at
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

    async def find_confirmed_workspace_by_installation_id(
        self,
        connection: asyncpg.Connection,
        *,
        installation_id: int,
    ) -> UUID | None:
        """Which workspace GitHub has confirmed owns this installation.

        One of three statements here that are not scoped, because they are
        what *produces* a scope: a webhook delivery names an installation and
        nothing else, so there is no workspace to pass in until one has run.

        What stands in for a scope is three things. The caller has already
        verified GitHub's HMAC over the raw body, so the installation id is
        one GitHub sent rather than one a client chose;
        `github_installations_installation_id_key` makes the answer at most
        one workspace, so a delivery can never fan out across tenants; and
        `confirmed_at IS NOT NULL` is the predicate this method is named for.

        That predicate is the fix, and it is one line because it is the choke
        point: every webhook write in this feature routes through here, so
        excluding unconfirmed claims here excludes them everywhere rather than
        in each caller that remembers. Without it a row that says only "some
        workspace typed this number into a callback" is enough to receive
        another organisation's account name and private repository names.

        None means no workspace here holds a confirmed installation with that
        id -- an app installed into an account that never completed the
        callback, or a claim GitHub has not answered for yet. The caller
        either tries `confirm_installation` or drops the delivery.
        """
        # Annotated rather than returned inline: asyncpg is untyped, so
        # returning the call directly would satisfy any return type at all.
        workspace_id: UUID | None = await connection.fetchval(
            """
            SELECT workspace_id
            FROM github_installations
            WHERE installation_id = $1
              AND confirmed_at IS NOT NULL
            """,
            installation_id,
        )

        return workspace_id

    async def confirm_installation(
        self,
        connection: asyncpg.Connection,
        *,
        installation_id: int,
        within: timedelta,
    ) -> UUID | None:
        """Promote a young, unconfirmed claim on this id, and say whose it was.

        The only writer of `confirmed_at`, reached only from a delivery whose
        signature the transport has already verified. That is the entire
        argument for trusting the result: GitHub is the one party that can say
        which workspace's browser really installed the app, and a signed
        delivery naming this installation *while the claim is still open* is
        the closest this deployment can get to hearing it say so.

        Three predicates, and each one refuses a different attack:

        * `confirmed_at IS NULL` -- an already-confirmed installation is not
          re-confirmed, so a delivery cannot move a live link;
        * `connected_at > now() - within` -- a claim on an installation
          created long ago is never promoted, because the deliveries that
          could promote it were sent and dropped before the claim existed.
          This is what turns "name any installation id, ever" into "predict
          one and race a live install", and it is the whole defence;
        * `installation_id = $1` with 013's unique constraint -- at most one
          claim can be promoted, so two workspaces cannot both win.

        The interval is a parameter rather than a literal so that the deadline
        stays a policy in `app.services.github` and is compared against the
        server's own clock -- `connected_at` is written by `now()`, and mixing
        it with a timestamp computed in Python would compare two clocks.

        None means there was no such claim, which is ordinary: a delivery for
        an installation nobody has claimed, or one whose claim has expired.
        """
        workspace_id: UUID | None = await connection.fetchval(
            """
            UPDATE github_installations
            SET confirmed_at = now(), updated_at = now()
            WHERE installation_id = $1
              AND confirmed_at IS NULL
              AND connected_at > now() - $2::interval
            RETURNING workspace_id
            """,
            installation_id,
            within,
        )

        return workspace_id

    async def delete_expired_claim(
        self,
        connection: asyncpg.Connection,
        *,
        installation_id: int,
        older_than: timedelta,
    ) -> bool:
        """Drop an unconfirmed claim on this id that has run out of time.

        Unscoped, and this one is a write, so it is worth being explicit about
        what keeps it safe. It can only ever delete a row that is unconfirmed
        AND older than the window -- which is to say a row that can no longer
        become a connection, holds no account login
        (github_installations_unconfirmed_holds_no_account) and therefore has
        no repositories to orphan. A confirmed installation is untouchable by
        it whatever id is passed.

        It exists because the alternative is a permanent denial of service.
        013's unique constraint is what stops two workspaces claiming one
        installation, and it cannot distinguish a live claim from a dead one:
        without this, a single unconfirmed claim would lock the real owner out
        of connecting for the lifetime of the database. A live claim still
        wins -- the loser gets a clean refusal it can retry after the window.

        A sweep on the claim path rather than a background job: the only
        moment a stale claim matters is when somebody wants the id, and a job
        would be a scheduler, a lock and a deployment concern for a delete
        that costs one index probe here.
        """
        row = await connection.fetchrow(
            """
            DELETE FROM github_installations
            WHERE installation_id = $1
              AND confirmed_at IS NULL
              AND connected_at <= now() - $2::interval
            RETURNING workspace_id
            """,
            installation_id,
            older_than,
        )

        return row is not None

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
            confirmed_at=row["confirmed_at"],
        )

    @staticmethod
    def _to_repository(row: asyncpg.Record) -> GithubRepositoryEntity:
        return GithubRepositoryEntity(
            repository_id=row["repository_id"],
            full_name=row["full_name"],
        )
