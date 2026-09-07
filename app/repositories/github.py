from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import asyncpg

from app.domain.errors import GithubInstallationClaimedError
from app.domain.github import (
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
        """
        found = await connection.fetchval(
            """
            SELECT 1
            FROM github_repositories
            WHERE workspace_id = $1 AND repository_id = $2
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

    @staticmethod
    def _to_repository(row: asyncpg.Record) -> GithubRepositoryEntity:
        return GithubRepositoryEntity(
            repository_id=row["repository_id"],
            full_name=row["full_name"],
        )
