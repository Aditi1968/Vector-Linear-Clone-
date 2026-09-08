from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import asyncpg

from app.domain.errors import GithubInstallationClaimedError
from app.domain.github import (
    GithubAutomationEntity,
    GithubCommitEntity,
    GithubInstallationEntity,
    GithubPullRequestEntity,
    GithubRepositoryEntity,
)
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
            SELECT repository_id, full_name, tracked
            FROM github_repositories
            WHERE workspace_id = $1
            ORDER BY full_name
            """,
            scope.workspace_id,
        )

        return [self._to_repository(row) for row in rows]

    async def set_tracked_repositories(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_ids: Sequence[int],
    ) -> None:
        """Make the tracked set exactly `repository_ids`. One statement.

        The whole set rather than one repository per call, because the setting
        is a list of checkboxes an admin submits together and a per-repository
        toggle would make "untrack these three" three round trips, three
        chances to half-apply, and three separate answers to the release check
        the service performs once for the set.

        An id naming a repository this workspace does not cover simply matches
        nothing -- there is no INSERT here, only an UPDATE over rows the
        workspace already has -- so a client cannot use this to learn whether
        another tenant covers a repository, and cannot create coverage it was
        never granted.

        `tracked = (repository_id = ANY(...))` writes both halves in one pass:
        the ids named are tracked and every other row of this workspace is not.
        Two statements would leave an instant at which nothing was tracked, and
        a delivery landing in it would be dropped.
        """
        await connection.execute(
            """
            UPDATE github_repositories
            SET tracked = (repository_id = ANY($2::BIGINT[]))
            WHERE workspace_id = $1
              AND tracked IS DISTINCT FROM (repository_id = ANY($2::BIGINT[]))
            """,
            scope.workspace_id,
            list(repository_ids),
        )

    async def repositories_with_releases(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_ids: Sequence[int],
    ) -> bool:
        """Whether any of these repositories has a release recorded against it.

        The probe behind the untracking refusal, and it exists because
        untracking is an UPDATE where connect and disconnect are DELETEs:
        `releases_repository_fk` is RESTRICT and refuses those two outright,
        but a boolean flip trips no constraint at all. The rule is the same one
        migration 024 argues for -- shipping history must not be silently
        detached by a toggle elsewhere -- so it is enforced here rather than
        left to a schema that cannot see it.

        Answers a bool and names no release. Which repositories a workspace
        holds is already admin-only and `GithubRepositoriesInUseError`
        deliberately enumerates nothing; a count would be a second place
        deciding who may read that.
        """
        if not repository_ids:
            return False

        found = await connection.fetchval(
            """
            SELECT 1
            FROM releases
            WHERE workspace_id = $1
              AND repository_id = ANY($2::BIGINT[])
            LIMIT 1
            """,
            scope.workspace_id,
            list(repository_ids),
        )

        return found is not None

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

    # --- deliveries ----------------------------------------------------

    async def record_delivery(
        self,
        connection: asyncpg.Connection,
        *,
        delivery_id: str,
        event: str,
    ) -> bool:
        """Claim this delivery id. False means it has already been applied.

        The fourth unscoped statement here, and the one that could not be
        scoped even in principle: it runs BEFORE the routing lookup that would
        say which workspace the delivery belongs to, which is the point --
        doing that lookup for a redelivery already applied is exactly the work
        `github_deliveries` exists to avoid. A delivery id is GitHub's own
        value and unique across all of GitHub, so a global key is the honest
        shape; see the table's note in 017.

        What stands in for a scope is the signature: the transport has already
        verified GitHub's HMAC over the raw body, so this id is one GitHub
        stamped rather than one a client chose.

        `ON CONFLICT DO NOTHING` rather than catching UniqueViolationError.
        The caller runs this as the first statement of the delivery's
        transaction, and a raised constraint violation would abort that
        transaction -- so the duplicate could not then be answered with the
        2xx that stops GitHub retrying it forever.

        Recorded inside the caller's transaction rather than before it, which
        is what makes a failed application retryable: if the writes that
        follow raise, this row rolls back with them and GitHub's redelivery is
        a first attempt again. The alternative -- committing the id up front --
        turns any transient failure into a delivery that is permanently lost.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO github_deliveries (delivery_id, event)
            VALUES ($1, $2)
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING delivery_id
            """,
            delivery_id,
            event,
        )

        return row is not None

    # --- development activity ------------------------------------------

    async def repository_exists(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
    ) -> bool:
        """Whether this workspace's installation covers this repository.

        Asked before any pull request or commit is written, and not for
        tidiness: `github_pull_requests_repository_fk` would refuse the row
        anyway, but a ForeignKeyViolationError aborts the delivery's
        transaction and reaches the transport as a 500 -- which GitHub answers
        by redelivering, forever, a payload that will never succeed. A miss
        here is a silent drop instead.

        The workspace leads the predicate, so this cannot answer for another
        tenant's coverage of the same repository -- and two tenants covering
        one repository is legitimate, which is why
        `github_repositories_repository_id_idx` is not unique.

        `tracked` is the second half of the predicate and the single point at
        which repository selection takes effect. Both development events --
        `pull_request` and `push` -- pass through here before they write
        anything, so an untracked repository is answered exactly as one the
        installation never covered: the delivery is dropped in silence, having
        written nothing. One predicate in one place rather than a check in each
        handler, which is the shape where the second handler forgets.
        """
        found = await connection.fetchval(
            """
            SELECT 1
            FROM github_repositories
            WHERE workspace_id = $1 AND repository_id = $2 AND tracked
            """,
            scope.workspace_id,
            repository_id,
        )

        return found is not None

    async def resolve_issue_ids(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        identifiers: Sequence[tuple[str, int]],
    ) -> dict[tuple[str, int], UUID]:
        """Which of these `ENG-142` pairs name a live issue in THIS workspace.

        The statement migrations/017_github_development.sql calls "the
        service's job", and the sentence that matters is the one it does not
        contain: there is no parameter for a workspace other than the scope's.
        `$1` is bound into both joins, so an identifier naming another
        tenant's team resolves to zero rows here rather than to that tenant's
        issue -- the resolver does not merely refuse to store the link, it
        never finds the id to store. The composite foreign keys in 017 are the
        floor under that, not the mechanism.

        Both joins are equality on unique indexes -- teams_workspace_key_unique
        for the key, issues_team_number_key for the number -- so a title
        offering twenty identifiers costs twenty index probes in one round
        trip rather than twenty statements.

        `archived_at IS NULL` for the reason every issue read carries it: an
        archived issue is not in the product, and a pull-request title is not
        the way back in.

        Keyed by the identifier rather than a flat list of ids, because the
        caller has to partition them again: a title and a branch name name
        different sets, and they are stored and retracted separately. One
        statement for the union is what makes that partition free -- three
        separate resolutions would be three round trips inside the delivery's
        transaction for a question one answers.

        An identifier that names nothing is simply absent from the result. It
        is the ordinary case: a pull-request title saying "ENG-142" on a
        repository whose workspace has no ENG team is a mention of somebody
        else's tracker, not an error.

        Ids only, per identifier. The caller writes link rows and never
        renders an issue from here, so returning entities would be a wider
        SELECT for columns nothing reads.
        """
        if not identifiers:
            return {}

        rows = await connection.fetch(
            """
            SELECT wanted.team_key, wanted.number, issues.id
            FROM unnest($2::TEXT[], $3::BIGINT[]) AS wanted(team_key, number)
            JOIN teams
              ON teams.workspace_id = $1
             AND teams.key = wanted.team_key
            JOIN issues
              ON issues.workspace_id = $1
             AND issues.team_id = teams.id
             AND issues.number = wanted.number
            WHERE issues.archived_at IS NULL
            """,
            scope.workspace_id,
            [team_key for team_key, _ in identifiers],
            [number for _, number in identifiers],
        )

        return {(row["team_key"], row["number"]): row["id"] for row in rows}

    async def upsert_pull_request(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        number: int,
        title: str,
        state: str,
        draft: bool,
        merged_at: datetime | None,
        head_ref: str | None,
        url: str | None,
        github_updated_at: datetime | None,
    ) -> bool:
        """Write this pull request. False means the payload was already stale.

        The `WHERE` on the conflict branch is the out-of-order defence, and it
        is in SQL rather than in a read-then-write for the reason 005 gives
        about issue numbers: two deliveries for one pull request can be in
        flight at once, and a SELECT-then-UPDATE decides staleness against a
        row another transaction is free to change before the UPDATE lands.

        `github_updated_at` and not arrival order, because arrival order is
        not a fact about the pull request: GitHub retries, queues and
        redelivers, so the payload that arrives second is routinely the older
        one. A row with no stored timestamp is always overwritten (there is
        nothing to be older than), and a payload with no timestamp never
        overwrites one that has (it cannot prove it is newer).

        `>=` rather than `>`: a redelivery of the same payload writes the same
        values, which is idempotent, and two events GitHub stamped identically
        -- which redeliveries of one edit routinely are -- must not deadlock
        into neither applying.

        Returns whether the row now reflects this payload. False is the
        caller's signal to leave the issue links alone: re-deriving them from
        a stale title would retract links the current title still supports.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO github_pull_requests (
                workspace_id,
                repository_id,
                number,
                title,
                state,
                draft,
                merged_at,
                head_ref,
                url,
                github_updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (workspace_id, repository_id, number) DO UPDATE
            SET title = EXCLUDED.title,
                state = EXCLUDED.state,
                draft = EXCLUDED.draft,
                merged_at = EXCLUDED.merged_at,
                head_ref = EXCLUDED.head_ref,
                url = EXCLUDED.url,
                github_updated_at = EXCLUDED.github_updated_at,
                updated_at = now()
            WHERE github_pull_requests.github_updated_at IS NULL
               OR (
                    EXCLUDED.github_updated_at IS NOT NULL
                    AND EXCLUDED.github_updated_at
                        >= github_pull_requests.github_updated_at
                  )
            RETURNING number
            """,
            scope.workspace_id,
            repository_id,
            number,
            title,
            state,
            draft,
            merged_at,
            head_ref,
            url,
            github_updated_at,
        )

        return row is not None

    async def set_pull_request_links(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        number: int,
        source: str,
        issue_ids: Sequence[UUID],
    ) -> None:
        """Make this ONE source's links exactly `issue_ids`.

        Two statements, both filtered by `source`, and that filter is the
        whole reason `source` is inside the primary key in 017: editing a
        title has to retract what the title said and leave what the branch
        name still says. A delete without it would take the branch's link with
        the title's.

        `NOT (issue_id = ANY(...))` rather than deleting the source's rows
        outright, so a link that survives the edit keeps its `created_at` --
        "linked since" is a fact about the link, not about the last delivery
        that mentioned it. An empty array deletes every row for the source,
        which is what an edit that removed the last identifier means.

        The insert's `ON CONFLICT DO NOTHING` covers the redelivery: the same
        payload twice writes the same set, and the second one is a no-op
        rather than a primary key violation that would abort the delivery.

        Every id in `issue_ids` came from `resolve_issue_ids` under this same
        scope. Nothing here re-checks that, because the composite foreign keys
        in 017 do: an id from another workspace has no column to go in.
        """
        wanted = list(issue_ids)

        await connection.execute(
            """
            DELETE FROM github_pull_request_issues
            WHERE workspace_id = $1
              AND repository_id = $2
              AND number = $3
              AND source = $4
              AND NOT (issue_id = ANY($5::UUID[]))
            """,
            scope.workspace_id,
            repository_id,
            number,
            source,
            wanted,
        )

        if not wanted:
            return

        await connection.execute(
            """
            INSERT INTO github_pull_request_issues (
                workspace_id, repository_id, number, issue_id, source
            )
            SELECT $1, $2, $3, incoming.issue_id, $4
            FROM unnest($5::UUID[]) AS incoming(issue_id)
            ON CONFLICT DO NOTHING
            """,
            scope.workspace_id,
            repository_id,
            number,
            source,
            wanted,
        )

    async def add_commit(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        sha: str,
        message: str,
        url: str | None,
        committed_at: datetime | None,
    ) -> None:
        """Record a commit this push reported. Idempotent.

        `DO NOTHING` rather than the pull request's `DO UPDATE`, because a
        commit is immutable: the same SHA is the same content by construction,
        so a second delivery has nothing new to say and there is no
        out-of-order question to answer. A force-push that removes the commit
        from the branch does not un-author it, and the row stays.
        """
        await connection.execute(
            """
            INSERT INTO github_commits (
                workspace_id, repository_id, sha, message, url, committed_at
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT DO NOTHING
            """,
            scope.workspace_id,
            repository_id,
            sha,
            message,
            url,
            committed_at,
        )

    async def add_commit_links(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        sha: str,
        issue_ids: Sequence[UUID],
    ) -> None:
        """Link a commit to issues its message named. Additive, never retracting.

        The commit-side counterpart of `set_pull_request_links`, and
        deliberately not its equal: a commit message cannot be edited, so
        there is no source to re-derive and no link an edit could withdraw.
        That is also why `github_commit_issues` carries no `source` column.
        """
        wanted = list(issue_ids)

        if not wanted:
            return

        await connection.execute(
            """
            INSERT INTO github_commit_issues (
                workspace_id, repository_id, sha, issue_id
            )
            SELECT $1, $2, $3, incoming.issue_id
            FROM unnest($4::UUID[]) AS incoming(issue_id)
            ON CONFLICT DO NOTHING
            """,
            scope.workspace_id,
            repository_id,
            sha,
            wanted,
        )

    async def delete_development(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_ids: Sequence[int] | None = None,
    ) -> None:
        """Remove the development activity of some or all of this workspace's
        repositories, children first.

        Four statements in exactly this order, because every foreign key in
        017 is RESTRICT: the two join tables point at the two activity tables,
        which point at `github_repositories`. Deleting the parent first is a
        RestrictViolationError, and RESTRICT is what 017 chose precisely so
        that a one-line delete cannot discard this history while reporting
        `DELETE 1`. The caller runs all four inside one transaction, so there
        is no instant at which a link row survives the pull request it names.

        `repository_ids=None` means all of them -- what disconnecting and
        replacing the whole set need -- and a list means exactly those, which
        is what `installation_repositories.removed` needs. One method for both
        because the alternative is two whose SQL differs by a predicate, and
        two places for the tenant filter to be forgotten in;
        `delete_repositories` above is shaped the same way for the same reason.
        """
        selected = None if repository_ids is None else list(repository_ids)

        for statement in (
            """
            DELETE FROM github_pull_request_issues
            WHERE workspace_id = $1
              AND ($2::BIGINT[] IS NULL OR repository_id = ANY($2::BIGINT[]))
            """,
            """
            DELETE FROM github_commit_issues
            WHERE workspace_id = $1
              AND ($2::BIGINT[] IS NULL OR repository_id = ANY($2::BIGINT[]))
            """,
            """
            DELETE FROM github_pull_requests
            WHERE workspace_id = $1
              AND ($2::BIGINT[] IS NULL OR repository_id = ANY($2::BIGINT[]))
            """,
            """
            DELETE FROM github_commits
            WHERE workspace_id = $1
              AND ($2::BIGINT[] IS NULL OR repository_id = ANY($2::BIGINT[]))
            """,
        ):
            await connection.execute(statement, scope.workspace_id, selected)

    async def list_pull_requests_for_issue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        limit: int,
    ) -> list[GithubPullRequestEntity]:
        """The pull requests linked to one issue, newest activity first.

        Driven from the join table, which is the direction
        `github_pull_request_issues_workspace_issue_idx` exists for -- both
        join tables' primary keys answer the opposite question, and 017
        creates that index because this panel would otherwise scan every link
        row in the workspace on every issue open.

        `array_agg(source)` rather than a second query or one row per source:
        a pull request linked by both its title and its branch is one pull
        request with two reasons, and returning it twice would make the panel
        render it twice.

        `NULLS LAST` on the sort, because a row whose `github_updated_at` is
        NULL is one GitHub told us nothing about the age of -- putting it
        first would rank an unknown above every known. `number DESC` makes the
        order total, which matters here: redeliveries of one edit share an
        `updated_at`, and without a tie-break the panel reshuffles between
        reads.
        """
        rows = await connection.fetch(
            """
            SELECT
                pulls.repository_id,
                repositories.full_name,
                pulls.number,
                pulls.title,
                pulls.state,
                pulls.draft,
                pulls.merged_at,
                pulls.head_ref,
                pulls.url,
                pulls.github_updated_at,
                array_agg(links.source ORDER BY links.source) AS sources
            FROM github_pull_request_issues AS links
            JOIN github_pull_requests AS pulls
              ON pulls.workspace_id = links.workspace_id
             AND pulls.repository_id = links.repository_id
             AND pulls.number = links.number
            JOIN github_repositories AS repositories
              ON repositories.workspace_id = pulls.workspace_id
             AND repositories.repository_id = pulls.repository_id
            WHERE links.workspace_id = $1
              AND links.issue_id = $2
            GROUP BY
                pulls.workspace_id,
                pulls.repository_id,
                pulls.number,
                repositories.full_name
            ORDER BY pulls.github_updated_at DESC NULLS LAST, pulls.number DESC
            LIMIT $3
            """,
            scope.workspace_id,
            issue_id,
            limit,
        )

        return [self._to_pull_request(row) for row in rows]

    async def list_commits_for_issue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        limit: int,
    ) -> list[GithubCommitEntity]:
        """The commits linked to one issue, newest first.

        `NULLS LAST` and a total order for the reasons the pull-request read
        gives; the tie-break is the SHA, which is unique within a repository
        by construction.
        """
        rows = await connection.fetch(
            """
            SELECT
                commits.repository_id,
                repositories.full_name,
                commits.sha,
                commits.message,
                commits.url,
                commits.committed_at
            FROM github_commit_issues AS links
            JOIN github_commits AS commits
              ON commits.workspace_id = links.workspace_id
             AND commits.repository_id = links.repository_id
             AND commits.sha = links.sha
            JOIN github_repositories AS repositories
              ON repositories.workspace_id = commits.workspace_id
             AND repositories.repository_id = commits.repository_id
            WHERE links.workspace_id = $1
              AND links.issue_id = $2
            ORDER BY
                commits.committed_at DESC NULLS LAST,
                commits.repository_id,
                commits.sha
            LIMIT $3
            """,
            scope.workspace_id,
            issue_id,
            limit,
        )

        return [self._to_commit(row) for row in rows]

    @staticmethod
    def _to_pull_request(row: asyncpg.Record) -> GithubPullRequestEntity:
        return GithubPullRequestEntity(
            repository_id=row["repository_id"],
            repository_full_name=row["full_name"],
            number=row["number"],
            title=row["title"],
            state=row["state"],
            draft=row["draft"],
            merged_at=row["merged_at"],
            head_ref=row["head_ref"],
            url=row["url"],
            github_updated_at=row["github_updated_at"],
            link_sources=tuple(row["sources"]),
        )

    @staticmethod
    def _to_commit(row: asyncpg.Record) -> GithubCommitEntity:
        return GithubCommitEntity(
            repository_id=row["repository_id"],
            repository_full_name=row["full_name"],
            sha=row["sha"],
            message=row["message"],
            url=row["url"],
            committed_at=row["committed_at"],
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

    # --- status automations ---------------------------------------------

    async def list_automations(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> list[GithubAutomationEntity]:
        """Every team in this workspace that has configured an automation.

        Unpaginated, for the reason `list_repositories` is: this is at most one
        row per team, rendered as one list on one settings page, and the
        primary key's leading `workspace_id` serves the predicate.

        Teams with no row are simply absent. The settings screen reads the team
        list from `teams` and joins; there is no row here meaning "off",
        because the absence of one is what off is.
        """
        rows = await connection.fetch(
            """
            SELECT team_id, started_state_id, completed_state_id
            FROM github_issue_automations
            WHERE workspace_id = $1
            ORDER BY team_id
            """,
            scope.workspace_id,
        )

        return [
            GithubAutomationEntity(
                team_id=row["team_id"],
                started_state_id=row["started_state_id"],
                completed_state_id=row["completed_state_id"],
            )
            for row in rows
        ]

    async def find_states(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        state_ids: Sequence[UUID],
    ) -> dict[UUID, str]:
        """The category of each of these states, for states on THIS team.

        Keyed by id and holding `workflow_states.type`, which is the only thing
        the caller asks: whether the state an admin chose for the started slot
        is actually a started state.

        Scoped through (workspace_id, team_id), so a state belonging to another
        team -- or another tenant -- is simply absent from the result and the
        service reports it exactly as it reports one that does not exist.
        `github_issue_automations_started_state_fk` would refuse the row anyway;
        this is what turns that refusal into a field error rather than a
        ForeignKeyViolationError reaching a client as "Internal server error".
        """
        wanted = [state_id for state_id in state_ids if state_id is not None]

        if not wanted:
            return {}

        rows = await connection.fetch(
            """
            SELECT id, type
            FROM workflow_states
            WHERE workspace_id = $1
              AND team_id = $2
              AND id = ANY($3::UUID[])
            """,
            scope.workspace_id,
            team_id,
            wanted,
        )

        return {row["id"]: row["type"] for row in rows}

    async def default_state_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        category: str,
    ) -> UUID | None:
        """The state this team's board puts first in a category, or nothing.

        THE default derived from category, and the only place a state is ever
        chosen for a workspace rather than by one. It runs when an admin turns
        the automation on without naming states, and what it writes is stored
        explicitly -- so the delivery path never resolves a category, and a
        team that later adds a second `started` state does not silently change
        what its automation does.

        `ORDER BY position, id` is 005's own total order for a board, restated
        rather than invented: `workflow_states_position` is deliberately not
        unique, so `id` is what makes the answer the same on every call.

        None when the team has no state of that category -- which 005's seed
        makes unusual but which a team that deleted one is in. The caller
        leaves that half of the automation unset, and the trigger then does
        nothing, which is the honest answer rather than a guess at a state of
        some other category.
        """
        # Annotated rather than returned inline: `fetchval` answers Any, and
        # returning it directly would satisfy any return type this method
        # declared -- including the `str` a `type` column would give if the
        # SELECT list were ever edited.
        found: UUID | None = await connection.fetchval(
            """
            SELECT id
            FROM workflow_states
            WHERE workspace_id = $1 AND team_id = $2 AND type = $3
            ORDER BY position, id
            LIMIT 1
            """,
            scope.workspace_id,
            team_id,
            category,
        )

        return found

    async def upsert_automation(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        started_state_id: UUID | None,
        completed_state_id: UUID | None,
    ) -> None:
        """Store this team's automation, replacing whatever it had."""
        await connection.execute(
            """
            INSERT INTO github_issue_automations (
                workspace_id, team_id, started_state_id, completed_state_id
            )
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (workspace_id, team_id) DO UPDATE
            SET started_state_id = EXCLUDED.started_state_id,
                completed_state_id = EXCLUDED.completed_state_id,
                updated_at = now()
            """,
            scope.workspace_id,
            team_id,
            started_state_id,
            completed_state_id,
        )

    async def delete_automation(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
    ) -> None:
        """Turn this team's automation off. Idempotent.

        A delete rather than a flag, because the absence of a row IS off; see
        the note on `github_issue_automations` in migration 030.
        """
        await connection.execute(
            """
            DELETE FROM github_issue_automations
            WHERE workspace_id = $1 AND team_id = $2
            """,
            scope.workspace_id,
            team_id,
        )

    async def move_issues_for_automation(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
        category: str,
        from_categories: Sequence[str],
    ) -> list[tuple[UUID, UUID, UUID]]:
        """Move these issues to their own team's configured state for `category`.

        One statement for every issue a pull request links to, across every
        team those issues belong to. A pull request titled "Fixes ENG-1, WEB-3"
        names two teams with two boards and two configurations, so the target
        state cannot be an argument -- it is joined per issue, from the row that
        team wrote.

        Returns `(issue_id, from_state_id, to_state_id)` for the issues that
        ACTUALLY moved, which is what makes the caller's history and
        notification writes exact: an issue the predicates excluded produces no
        tuple and therefore no activity row, no notification and no event.

        Every predicate below is doing a distinct job.

        * `issues.workspace_id = $1` -- the tenant filter, leading, as every
          statement in this file does. The ids came from `resolve_issue_ids`
          under this same scope; this is the floor under that, not a repetition
          of it.
        * `archived_at IS NULL` -- an archived issue is not in the product, and
          a pull request is not the way back in. The same rule
          `resolve_issue_ids` applies, restated because an issue can be
          archived between the two statements.
        * `current.type = ANY($4)` -- forward only. See
          `app.domain.github.categories_below`: this is what stops the
          automation dragging an issue back out of a state a person moved it
          to, and what makes a redelivery a no-op without a dedupe table.
        * `issues.workflow_state_id <> target.id` -- an issue already sitting
          in the target writes nothing at all, so `updated_at` does not move
          and the row is not touched. Strictly redundant given the category
          predicate when the target's own category is excluded from
          `from_categories`, and kept because it is the one that stays true if
          the ladder ever changes.

        `completed_at` is recomputed by the same expression `IssueRepository`
        uses, and it has to be: the rule -- non-NULL if and only if the state
        is terminal, keeping the instant already there when moving between two
        terminal categories -- lives at the write and not in a constraint,
        because the two columns are in different tables. A move performed here
        that skipped it would leave an issue in Done with no completion
        instant, which every report over shipping dates would then be wrong
        about.
        """
        wanted = list(issue_ids)

        if not wanted:
            return []

        rows = await connection.fetch(
            """
            UPDATE issues
            SET workflow_state_id = target.id,
                completed_at = CASE
                    WHEN target.type IN ('completed', 'canceled')
                        THEN COALESCE(issues.completed_at, now())
                    ELSE NULL
                END,
                updated_at = now()
            FROM github_issue_automations AS automation
            JOIN workflow_states AS target
              ON target.workspace_id = automation.workspace_id
             AND target.team_id = automation.team_id
             AND target.id = CASE $3::TEXT
                    WHEN 'started' THEN automation.started_state_id
                    ELSE automation.completed_state_id
                END
            JOIN workflow_states AS current
              ON current.workspace_id = automation.workspace_id
             AND current.team_id = automation.team_id
            WHERE issues.workspace_id = $1
              AND issues.id = ANY($2::UUID[])
              AND issues.archived_at IS NULL
              AND automation.workspace_id = issues.workspace_id
              AND automation.team_id = issues.team_id
              AND current.id = issues.workflow_state_id
              AND current.type = ANY($4::TEXT[])
              AND issues.workflow_state_id <> target.id
            RETURNING issues.id, current.id AS from_state_id, target.id AS to_state_id
            """,
            scope.workspace_id,
            wanted,
            category,
            list(from_categories),
        )

        return [(row["id"], row["from_state_id"], row["to_state_id"]) for row in rows]

    @staticmethod
    def _to_repository(row: asyncpg.Record) -> GithubRepositoryEntity:
        return GithubRepositoryEntity(
            repository_id=row["repository_id"],
            full_name=row["full_name"],
            tracked=row["tracked"],
        )
