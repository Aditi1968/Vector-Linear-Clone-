from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.releases import (
    CommitBoundary,
    EnvironmentEntity,
    ReleaseEntity,
    ReleaseIssueRef,
    ReleasePullRequestRef,
    ReleaseRange,
)
from app.domain.tenancy import WorkspaceScope


# `list[...]` is not spellable inside the class below, and the reason is the
# one app/repositories/projects.py states in full: the class has a method
# called `list`, so from that `def` onwards the name `list` in the class body
# IS the method, and `-> list[ReleaseEntity]` on a method below it is a
# subscript of a function. Aliases resolved out here, where `list` is still the
# builtin, fix both the import-time TypeError and mypy.
Releases = list[ReleaseEntity]
Environments = list[EnvironmentEntity]


# The columns every release read returns, including the two aggregates the
# entity carries.
#
# Correlated to the outer row rather than bound to the statement's parameters,
# so one constant serves the single-row reads and the list alike -- the shape
# app/repositories/initiatives.py establishes, and for its reason: it buys
# there being one definition of what a release row is, which is what stops a
# column added to the entity from being a KeyError on whichever read nobody
# exercised.
_RELEASE_COLUMNS = """
    releases.id,
    releases.name,
    releases.environment_id,
    releases.repository_id,
    releases.commit_sha,
    releases.previous_commit_sha,
    releases.status,
    releases.notes,
    releases.deployed_at,
    releases.created_at,
    releases.updated_at,
    (
        SELECT COALESCE(
            array_agg(shipped.issue_id ORDER BY shipped.issue_id),
            ARRAY[]::UUID[]
        )
        FROM release_issues shipped
        WHERE shipped.workspace_id = releases.workspace_id
            AND shipped.release_id = releases.id
    ) AS issue_ids,
    (
        SELECT COALESCE(
            array_agg(pulls.number ORDER BY pulls.number),
            ARRAY[]::INTEGER[]
        )
        FROM release_pull_requests pulls
        WHERE pulls.workspace_id = releases.workspace_id
            AND pulls.release_id = releases.id
    ) AS pull_request_numbers
"""

_ENVIRONMENT_COLUMNS = """
    id,
    name,
    kind,
    created_at
"""


class ReleaseRepository:
    """SQL access for `environments`, `releases` and the two release joins.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace, and the scope arrives as a
    required keyword argument -- the shape IssueRepository establishes and
    every repository since restates. `scope=` appears literally at every call
    site, so "does this query cross tenants" is answered by reading the call.

    Nothing here checks a row against its workspace before writing another that
    references it. Every foreign key migrations/024_releases.sql declares is
    composite over `workspace_id`, so PostgreSQL refuses a cross-workspace
    association as part of the statement itself. A SELECT-first check would be
    a second, weaker copy of that rule -- weaker because it is a separate
    statement the row can change between, and weaker because it would then be
    two places that have to agree.

    The one exception is `find_commit`, and it is an exception because
    `releases.commit_sha` deliberately carries no foreign key onto
    `github_commits` -- see the long note on that column in the migration. That
    method is a workspace-scoped lookup, so it cannot reach another tenant's
    commit; what it is checking is that the range resolves to something, which
    is not a tenancy question.
    """

    # ---------------------------------------------------------- environments

    async def list_environments(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> Environments:
        """Every environment this workspace has declared, by name.

        No cursor and no limit. A workspace has a handful of deploy targets --
        the list is a dropdown, not a feed -- and `environments_workspace_name_key`
        already bounds it far below anything paging would help with. Ordered by
        name so the dropdown does not reshuffle between reads.
        """
        rows = await connection.fetch(
            f"""
            SELECT {_ENVIRONMENT_COLUMNS}
            FROM environments
            WHERE workspace_id = $1
            ORDER BY name, id
            """,
            scope.workspace_id,
        )

        return [self._to_environment(row) for row in rows]

    async def create_environment(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        name: str,
        kind: str,
    ) -> EnvironmentEntity:
        """Declare one deploy target.

        `workspace_id` is written explicitly and carries no database default,
        so omitting it would be a NOT NULL violation rather than a quiet
        mis-filing. A duplicate name raises UniqueViolationError on
        `environments_workspace_name_key`, which the service names and
        translates.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO environments (workspace_id, name, kind)
            VALUES ($1, $2, $3)
            RETURNING {_ENVIRONMENT_COLUMNS}
            """,
            scope.workspace_id,
            name,
            kind,
        )

        return self._to_environment(row)

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        release_id: UUID,
    ) -> ReleaseEntity | None:
        """The release with this id in this workspace, or nothing.

        The workspace is part of the lookup rather than a check applied
        afterwards, so a release belonging to another tenant produces exactly
        the same answer as an id that exists nowhere. A caller holding a
        guessed or leaked id learns nothing by asking.
        """
        row = await connection.fetchrow(
            f"""
            SELECT {_RELEASE_COLUMNS}
            FROM releases
            WHERE releases.workspace_id = $1 AND releases.id = $2
            """,
            scope.workspace_id,
            release_id,
        )

        if row is None:
            return None

        return self._to_release(row)

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> Releases:
        """Keyset page of one workspace's releases, newest first.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison, and
        `workspace_id` leads both statements so a page is served by the leading
        columns of releases_workspace_created_at_id_idx.

        The tenant predicate is ANDed with the cursor rather than folded into
        it. Widening the row-value comparison to
        `(workspace_id, created_at, id) < (...)` would put workspaces into the
        ordering, which is how a page walk falls out of one tenant and into
        whichever one sorts next.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                f"""
                SELECT {_RELEASE_COLUMNS}
                FROM releases
                WHERE releases.workspace_id = $1
                ORDER BY releases.created_at DESC, releases.id DESC
                LIMIT $2
                """,
                scope.workspace_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                f"""
                SELECT {_RELEASE_COLUMNS}
                FROM releases
                WHERE releases.workspace_id = $1
                    AND (releases.created_at, releases.id) < ($2, $3)
                ORDER BY releases.created_at DESC, releases.id DESC
                LIMIT $4
                """,
                scope.workspace_id,
                after_created_at,
                after_id,
                limit,
            )

        return [self._to_release(row) for row in rows]

    # --------------------------------------------------------- the range

    async def find_commit(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        sha: str,
    ) -> CommitBoundary | None:
        """One end of a release's range, or nothing if this server never saw it.

        Scoped to (workspace, repository) rather than to the SHA alone, which
        is what makes this safe to run on a client-supplied value: a commit in
        another tenant's repository is not found, and the caller gets the same
        answer a SHA that exists nowhere produces. That indistinguishability is
        the reason `releases.commit_sha` can carry no foreign key without
        becoming a tenancy hole.
        """
        row = await connection.fetchrow(
            """
            SELECT sha, committed_at
            FROM github_commits
            WHERE workspace_id = $1 AND repository_id = $2 AND sha = $3
            """,
            scope.workspace_id,
            repository_id,
            sha,
        )

        if row is None:
            return None

        return CommitBoundary(sha=row["sha"], committed_at=row["committed_at"])

    async def previous_deployed_commit_sha(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        environment_id: UUID,
    ) -> str | None:
        """Where the last deploy of this repository into this environment left
        off, or None if there has never been one.

        `deployed_at IS NOT NULL` rather than `status = 'deployed'`, and the
        difference is the rollback: a withdrawn release DID reach the
        environment and its notes DID describe its range, so starting the next
        release from before it would repeat every entry. The predicate is
        exactly the left-hand side of `releases_deployed_at_matches_status`, so
        the two cannot drift.

        A release that failed is skipped, which is the other half of the same
        argument: it shipped nothing, so its range is still unaccounted for and
        the next release has to cover it.

        `deployed_at DESC, id DESC`, matching
        releases_workspace_repository_environment_deployed_idx exactly. The
        tie-break is not decoration: two deploys recorded in one transaction
        share an instant, and without it "the previous release" would depend on
        the scan order.
        """
        # Annotated rather than returned inline: asyncpg ships no types, so
        # `fetchval` is Any and returning it directly would satisfy any return
        # type at all -- including one this method later stopped producing.
        sha: str | None = await connection.fetchval(
            """
            SELECT commit_sha
            FROM releases
            WHERE workspace_id = $1
                AND repository_id = $2
                AND environment_id = $3
                AND deployed_at IS NOT NULL
            ORDER BY deployed_at DESC, id DESC
            LIMIT 1
            """,
            scope.workspace_id,
            repository_id,
            environment_id,
        )

        return sha

    async def resolve_range(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        repository_id: int,
        since: datetime | None,
        until: datetime,
        limit: int,
    ) -> ReleaseRange:
        """What one commit range contains, read out of migration 017's tables.

        Two statements rather than one, because the two halves ask different
        tables different questions and a join between them would multiply rows
        for no reader.

        ## What "the range" means, exactly

        `(since, until]` over an instant, not a walk of the commit graph.
        017 stores no parent SHAs -- a push payload's commits arrive as a flat
        list -- so `git rev-list A..B` is not a query this schema can answer,
        and the honest substitute is the window between the two boundary
        commits' own timestamps. `since` is None for the first release into an
        environment, which opens the window at the beginning of time.

        ponytail: a time window, not a true ancestry range. Two ceilings follow
        from it and both are real. A commit rebased onto the branch keeps its
        AUTHOR date, so it can sort before a boundary it actually came after;
        and `merged_at` (GitHub's clock) is compared against a window whose
        ends are `committed_at` (git's), so a squash-merged pull request can
        land just outside a window that contains its own commit. The upgrade is
        a `parent_shas TEXT[]` column on `github_commits` in a later migration
        and a recursive walk here; until then the window is what a deploy tool
        without a repository clone can compute, and the result is frozen at
        creation so it cannot drift afterwards.

        ## The issues

        A UNION of two sources, because an identifier can be linked from either
        side and neither alone is enough. A commit message naming ENG-142 puts
        the issue in `github_commit_issues`; a pull request whose TITLE names
        it puts it in `github_pull_request_issues` and may have no commit that
        mentions it at all -- which is the ordinary case for squash merges,
        where the individual commits say "fix typo". Taking only the commit
        side would silently drop the link source 017 documents as the most
        common one.

        `UNION` and not `UNION ALL`: an issue linked from both sides is one
        issue, and the de-duplication is the point rather than an accident.

        Both halves and the outer join carry `workspace_id = $1`. The join
        tables' composite foreign keys already guarantee the issue is in this
        tenant, so the outer predicate is redundant -- and it is written out
        anyway, because a redundant tenant filter costs an index lookup and its
        absence costs a tenant boundary the first time somebody edits this
        statement.

        `limit` bounds both reads. A release spanning a year of an active
        repository is a real thing to ask for by accident (a client sending
        `previousCommitSha: null` on a repository with ten thousand commits),
        and an unbounded read here would render it into a single TEXT column
        that `releases_notes_length` would then refuse -- after the work.
        """
        issue_rows = await connection.fetch(
            """
            WITH touched AS (
                SELECT commit_links.issue_id
                FROM github_commits AS commits
                JOIN github_commit_issues AS commit_links
                  ON commit_links.workspace_id = commits.workspace_id
                 AND commit_links.repository_id = commits.repository_id
                 AND commit_links.sha = commits.sha
                WHERE commits.workspace_id = $1
                  AND commits.repository_id = $2
                  AND commits.committed_at IS NOT NULL
                  AND ($3::TIMESTAMPTZ IS NULL OR commits.committed_at > $3)
                  AND commits.committed_at <= $4

                UNION

                SELECT pull_links.issue_id
                FROM github_pull_requests AS pulls
                JOIN github_pull_request_issues AS pull_links
                  ON pull_links.workspace_id = pulls.workspace_id
                 AND pull_links.repository_id = pulls.repository_id
                 AND pull_links.number = pulls.number
                WHERE pulls.workspace_id = $1
                  AND pulls.repository_id = $2
                  AND pulls.merged_at IS NOT NULL
                  AND ($3::TIMESTAMPTZ IS NULL OR pulls.merged_at > $3)
                  AND pulls.merged_at <= $4
            )
            SELECT
                issues.id,
                teams.key AS team_key,
                issues.number,
                issues.title
            FROM touched
            JOIN issues
              ON issues.workspace_id = $1
             AND issues.id = touched.issue_id
            JOIN teams
              ON teams.workspace_id = issues.workspace_id
             AND teams.id = issues.team_id
            ORDER BY teams.key, issues.number
            LIMIT $5
            """,
            scope.workspace_id,
            repository_id,
            since,
            until,
            limit,
        )

        pull_rows = await connection.fetch(
            """
            SELECT repository_id, number, title, url
            FROM github_pull_requests
            WHERE workspace_id = $1
              AND repository_id = $2
              AND merged_at IS NOT NULL
              AND ($3::TIMESTAMPTZ IS NULL OR merged_at > $3)
              AND merged_at <= $4
            ORDER BY number
            LIMIT $5
            """,
            scope.workspace_id,
            repository_id,
            since,
            until,
            limit,
        )

        return ReleaseRange(
            issues=tuple(self._to_issue_ref(row) for row in issue_rows),
            pull_requests=tuple(self._to_pull_ref(row) for row in pull_rows),
        )

    # ---------------------------------------------------------------- writes

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        name: str,
        environment_id: UUID,
        repository_id: int,
        commit_sha: str,
        previous_commit_sha: str | None,
        status: str,
        notes: str,
    ) -> ReleaseEntity:
        """Insert one release, with no links yet.

        `status` carries no database default, so the caller has to choose one;
        the schema names the legal statuses and refuses everything else but
        does not pick.

        `deployed_at` is deliberately absent from the column list. A release is
        created before it is deployed, and
        `releases_deployed_at_matches_status` refuses any status but 'pending'
        or 'failed' while it is NULL -- so a caller trying to create a release
        already marked deployed is refused by the database rather than by this
        method remembering to look.

        The two aggregates come back as empty array literals rather than from
        subqueries. A release one statement old has no links -- no statement
        anywhere has been able to reference its id yet -- so the subqueries
        could only ever return empty. The caller adds the links and re-reads.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO releases (
                workspace_id,
                name,
                environment_id,
                repository_id,
                commit_sha,
                previous_commit_sha,
                status,
                notes
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING
                id,
                name,
                environment_id,
                repository_id,
                commit_sha,
                previous_commit_sha,
                status,
                notes,
                deployed_at,
                created_at,
                updated_at,
                ARRAY[]::UUID[] AS issue_ids,
                ARRAY[]::INTEGER[] AS pull_request_numbers
            """,
            scope.workspace_id,
            name,
            environment_id,
            repository_id,
            commit_sha,
            previous_commit_sha,
            status,
            notes,
        )

        return self._to_release(row)

    async def add_issues(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        release_id: UUID,
        issue_ids: Sequence[UUID],
    ) -> None:
        """Freeze which issues this release shipped.

        One statement over an array parameter rather than a loop, so a release
        touching forty issues is one round trip.

        The single `$1` feeding both foreign keys is the whole cross-tenant
        mechanism: the row carries one workspace, so the release and the issue
        are checked against the same tenant. An issue from another workspace
        raises ForeignKeyViolationError on `release_issues_issue_fk`.

        No `ON CONFLICT DO NOTHING`. The ids come from `resolve_range`, whose
        UNION already de-duplicates them, and a duplicate reaching here would
        mean that de-duplication stopped working -- which is a defect to
        surface, not a collision to absorb.
        """
        wanted = list(issue_ids)

        if not wanted:
            return

        await connection.execute(
            """
            INSERT INTO release_issues (workspace_id, release_id, issue_id)
            SELECT $1, $2, incoming.issue_id
            FROM unnest($3::UUID[]) AS incoming(issue_id)
            """,
            scope.workspace_id,
            release_id,
            wanted,
        )

    async def add_pull_requests(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        release_id: UUID,
        pull_requests: Sequence[ReleasePullRequestRef],
    ) -> None:
        """Freeze which pull requests this release shipped, titles and all.

        Parallel arrays through one `unnest`, which keeps this a single
        statement. The columns are a snapshot rather than a reference --
        migrations/024_releases.sql argues for that at length -- so the title
        and url travel with the ids instead of being joined at read time.
        """
        wanted = list(pull_requests)

        if not wanted:
            return

        await connection.execute(
            """
            INSERT INTO release_pull_requests (
                workspace_id, release_id, repository_id, number, title, url
            )
            SELECT $1, $2, incoming.repository_id, incoming.number,
                   incoming.title, incoming.url
            FROM unnest($3::BIGINT[], $4::INTEGER[], $5::TEXT[], $6::TEXT[])
                AS incoming(repository_id, number, title, url)
            """,
            scope.workspace_id,
            release_id,
            [ref.repository_id for ref in wanted],
            [ref.number for ref in wanted],
            [ref.title for ref in wanted],
            [ref.url for ref in wanted],
        )

    async def set_status(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        release_id: UUID,
        status: str,
        allowed_from: Sequence[str],
        stamp_deployed_at: bool,
    ) -> ReleaseEntity | None:
        """Move a release to `status`, or return nothing if it may not move.

        `allowed_from` is IN THE STATEMENT rather than checked beforehand, and
        that is the whole design of this method. A read-then-write would leave
        a window in which two concurrent transitions both read 'pending' and
        both wrote -- the second overwriting a status the first had already
        committed. Here the losing transaction matches no row.

        The consequence is that a `None` return has two causes -- no such
        release in this workspace, and a release in a state this move is not
        legal from -- and the service tells them apart by re-reading inside the
        same transaction. That is the right way round: the ambiguity lives
        where a second read is cheap and correct, not in the statement that has
        to be atomic.

        `stamp_deployed_at` is passed rather than derived from `status` here,
        so the string 'deployed' appears in app/domain/releases.py and nowhere
        else. `releases_deployed_at_matches_status` is what actually enforces
        the pairing; this is what satisfies it.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE releases
            SET
                status = $3,
                deployed_at = CASE
                    WHEN $4::BOOLEAN THEN now() ELSE deployed_at
                END,
                updated_at = now()
            WHERE releases.workspace_id = $1
                AND releases.id = $2
                AND releases.status = ANY($5::TEXT[])
            RETURNING {_RELEASE_COLUMNS}
            """,
            scope.workspace_id,
            release_id,
            status,
            stamp_deployed_at,
            list(allowed_from),
        )

        if row is None:
            return None

        return self._to_release(row)

    async def clear_links(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        release_id: UUID,
    ) -> None:
        """Drop both link tables' rows for one release, before deleting it.

        Written out rather than delegated to ON DELETE CASCADE, for the reason
        migrations/024_releases.sql chose RESTRICT: a one-line delete must not
        discard the record of what a release shipped while the command tag
        reads `DELETE 1`.
        """
        for statement in (
            """
            DELETE FROM release_issues
            WHERE workspace_id = $1 AND release_id = $2
            """,
            """
            DELETE FROM release_pull_requests
            WHERE workspace_id = $1 AND release_id = $2
            """,
        ):
            await connection.execute(statement, scope.workspace_id, release_id)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        release_id: UUID,
    ) -> bool:
        """Delete the release, reporting whether there was one to delete.

        The workspace is in the predicate, so this cannot reach another
        tenant's release however the id was obtained.

        This does NOT remove the rows that reference it: both foreign keys onto
        `releases` are ON DELETE RESTRICT, so a release still carrying links
        makes the server refuse this statement. ReleaseService.delete calls
        `clear_links` first, in one transaction.
        """
        # Annotated rather than compared inline: asyncpg ships no types, so
        # `execute` is Any and `Any == str` would silently satisfy `bool`.
        status: str = await connection.execute(
            """
            DELETE FROM releases
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            release_id,
        )

        return status == "DELETE 1"

    # --------------------------------------------------------------- mapping

    @staticmethod
    def _to_release(row: asyncpg.Record) -> ReleaseEntity:
        # The two aggregates arrive as Python lists; the entity is frozen, so
        # they are copied into tuples rather than handed out as mutable aliases
        # of whatever asyncpg built.
        return ReleaseEntity(
            id=row["id"],
            name=row["name"],
            environment_id=row["environment_id"],
            repository_id=row["repository_id"],
            commit_sha=row["commit_sha"],
            previous_commit_sha=row["previous_commit_sha"],
            status=row["status"],
            notes=row["notes"],
            deployed_at=row["deployed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            issue_ids=tuple(row["issue_ids"]),
            pull_request_numbers=tuple(row["pull_request_numbers"]),
        )

    @staticmethod
    def _to_environment(row: asyncpg.Record) -> EnvironmentEntity:
        return EnvironmentEntity(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_issue_ref(row: asyncpg.Record) -> ReleaseIssueRef:
        return ReleaseIssueRef(
            issue_id=row["id"],
            team_key=row["team_key"],
            number=row["number"],
            title=row["title"],
        )

    @staticmethod
    def _to_pull_ref(row: asyncpg.Record) -> ReleasePullRequestRef:
        return ReleasePullRequestRef(
            repository_id=row["repository_id"],
            number=row["number"],
            title=row["title"],
            url=row["url"],
        )
