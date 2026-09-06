from uuid import UUID

import asyncpg

from app.domain.memberships import WorkspaceMemberEntity, WorkspaceMembershipEntity


class MembershipRepository:
    """SQL access for `workspace_members`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Both statements below project the same six columns and spell them out
    twice rather than sharing an interpolated fragment. Every statement in
    this repository is therefore a complete literal that can be read, pasted
    into psql and reasoned about on its own, and no SQL in this codebase is
    assembled from Python strings -- a property worth more than the six
    duplicated lines, because "this one interpolation is only a constant" is
    the shape every SQL injection starts as.

    The workspace columns are aliased. `workspaces.id` is unambiguous in SQL
    but arrives in an asyncpg.Record keyed by the bare column name, so an
    unaliased `workspaces.id` beside a future `workspace_members.id` would
    collide silently in `_to_entity`.
    """

    async def find_membership(
        self,
        connection: asyncpg.Connection,
        *,
        slug: str,
        user_id: UUID,
    ) -> WorkspaceMembershipEntity | None:
        """The caller's membership of the workspace with this slug, or nothing.

        One statement, and that is the security property rather than a
        performance note. Resolving the workspace first and then checking
        membership would compute, in this process, the difference between "no
        such workspace" and "not yours" -- and once a frame knows that
        difference it can leak it: into an error type, into a log line a
        support tool later exposes, into two response times a stranger can
        tell apart. Joining instead means the absent row has one meaning and
        the server never holds the other.

        The join is inner and the filter is two equalities, both bound as
        parameters. The slug comparison is exact for the reason spelled out on
        `WorkspaceRepository.find_id_by_slug`: `workspaces_slug_format`
        confines every stored slug to lowercase, so folding case here would be
        case-insensitive addressing of tenants.

        No LIMIT 1: `workspace_members_pkey` is on (workspace_id, user_id) and
        `workspaces_slug_key` makes a slug resolve to at most one workspace,
        so a second matching row is not a thing this schema can hold. A limit
        would claim doubt about a guarantee the schema already gives.

        Keyword-only, because these two parameters are the whole authorization
        question and transposing them at a call site is the one mistake here
        that would still typecheck at every layer above.
        """
        row = await connection.fetchrow(
            """
            SELECT
                workspaces.id AS workspace_id,
                workspaces.slug AS workspace_slug,
                workspaces.name AS workspace_name,
                workspace_members.user_id AS user_id,
                workspace_members.role AS role,
                workspace_members.created_at AS created_at
            FROM workspace_members
            JOIN workspaces
                ON workspaces.id = workspace_members.workspace_id
            WHERE workspaces.slug = $1
                AND workspace_members.user_id = $2
            """,
            slug,
            user_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def list_for_user(
        self,
        connection: asyncpg.Connection,
        *,
        user_id: UUID,
        limit: int,
    ) -> list[WorkspaceMembershipEntity]:
        """Every workspace this user belongs to, by slug, bounded by `limit`.

        Ordered by slug, which is a total order needing no tiebreak because
        `workspaces_slug_key` is unique. That matters beyond determinism: slug
        is the one column here whose order is stable under an update, so the
        day this list needs a cursor, the ordering it already has is the one
        the cursor can be built on.

        `limit` is required rather than defaulted, so the bound is a decision
        the service states out loud; see MEMBERSHIP_LIST_LIMIT for what it is
        and why it exists at all.

        The user id is the only filter. There is deliberately no workspace
        argument: this answers "which workspaces are mine", and a caller after
        one named workspace asks `find_membership`, which is the lookup that
        cannot distinguish absent from unauthorized.
        """
        rows = await connection.fetch(
            """
            SELECT
                workspaces.id AS workspace_id,
                workspaces.slug AS workspace_slug,
                workspaces.name AS workspace_name,
                workspace_members.user_id AS user_id,
                workspace_members.role AS role,
                workspace_members.created_at AS created_at
            FROM workspace_members
            JOIN workspaces
                ON workspaces.id = workspace_members.workspace_id
            WHERE workspace_members.user_id = $1
            ORDER BY workspaces.slug
            LIMIT $2
            """,
            user_id,
            limit,
        )

        return [self._to_entity(row) for row in rows]

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        user_id: UUID,
        role: str,
    ) -> WorkspaceMembershipEntity:
        """Grant a membership, and read back the workspace it grants.

        One statement, so the grant and the description of it come from the
        same snapshot. The alternative -- INSERT, then SELECT the workspace --
        is two round trips whose second one can be answered by a workspace row
        another transaction has since renamed.

        A data-modifying CTE rather than a plain `RETURNING`, because
        `workspace_members` holds no slug and no name: the caller needs both,
        and a join is the only way to get them alongside the row just written.
        A workspace inserted earlier in the caller's own transaction is
        visible to this statement, which is what lets workspace creation and
        the owner's grant be one transaction.

        No ON CONFLICT. A second grant for the same pair breaks
        `workspace_members_pkey`, and that refusal is the answer -- whether it
        means "already a member" is a question for the service.
        """
        row = await connection.fetchrow(
            """
            WITH granted AS (
                INSERT INTO workspace_members (workspace_id, user_id, role)
                VALUES ($1, $2, $3)
                RETURNING workspace_id, user_id, role, created_at
            )
            SELECT
                workspaces.id AS workspace_id,
                workspaces.slug AS workspace_slug,
                workspaces.name AS workspace_name,
                granted.user_id AS user_id,
                granted.role AS role,
                granted.created_at AS created_at
            FROM granted
            JOIN workspaces
                ON workspaces.id = granted.workspace_id
            """,
            workspace_id,
            user_id,
            role,
        )

        return self._to_entity(row)

    async def list_members(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        limit: int,
    ) -> list[WorkspaceMemberEntity]:
        """Everyone in one workspace, with the account behind each membership.

        The caller must already have established that the requester belongs to
        this workspace; a workspace id alone is not permission to read who is
        in it, and nothing in this statement checks. See
        `MembershipService.list_members`, the only caller, which takes an
        AuthorizedWorkspaceScope precisely so that the check cannot be skipped.

        Ordered by email, which is total without a tiebreak because
        `users_email_key` is unique -- `name` is nullable and not unique, so
        ordering by it would reshuffle the list between reads. A client that
        wants people sorted by display name sorts them.

        `limit` is required rather than defaulted, so the bound is a decision
        the service states out loud; see MEMBERSHIP_LIST_LIMIT.
        """
        rows = await connection.fetch(
            """
            SELECT
                workspace_members.user_id AS user_id,
                users.email AS email,
                users.name AS name,
                workspace_members.role AS role,
                workspace_members.created_at AS created_at
            FROM workspace_members
            JOIN users
                ON users.id = workspace_members.user_id
            WHERE workspace_members.workspace_id = $1
            ORDER BY users.email
            LIMIT $2
            """,
            workspace_id,
            limit,
        )

        return [self._to_member_entity(row) for row in rows]

    async def lock_owner_ids(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        owner_role: str,
    ) -> list[UUID]:
        """The workspace's owners, locked until the caller's transaction ends.

        FOR UPDATE is the whole point of this method, and the reason "never
        leave a workspace without an owner" cannot be a count taken before the
        write. Two transactions each demoting a different one of two owners
        both read a count of two, both decide they are safe, and both commit --
        leaving nobody. Under READ COMMITTED, PostgreSQL re-evaluates
        `role = $2` after acquiring the row lock, so the second transaction
        sees the first one's demotion and finds one owner rather than two.

        The caller MUST already be inside the transaction that performs the
        write. A lock taken in a transaction of its own is released before the
        write it was meant to protect.
        """
        rows = await connection.fetch(
            """
            SELECT user_id
            FROM workspace_members
            WHERE workspace_id = $1 AND role = $2
            FOR UPDATE
            """,
            workspace_id,
            owner_role,
        )

        return [row["user_id"] for row in rows]

    async def update_role(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        user_id: UUID,
        role: str,
    ) -> WorkspaceMemberEntity | None:
        """Set one member's role, or nothing if there is no such member here.

        Scoped by workspace as well as by user: without that predicate a user
        id from another tenant would have its role rewritten there, which is a
        cross-tenant write performed by a lookup that never looked anything up.

        Returns None rather than raising when nothing matched. Whether that
        means "no such account" or "not a member here" is a question for the
        service, and the repository does not answer questions about what an
        absence means.
        """
        row = await connection.fetchrow(
            """
            WITH updated AS (
                UPDATE workspace_members
                SET role = $3
                WHERE workspace_id = $1 AND user_id = $2
                RETURNING user_id, role, created_at
            )
            SELECT
                updated.user_id AS user_id,
                users.email AS email,
                users.name AS name,
                updated.role AS role,
                updated.created_at AS created_at
            FROM updated
            JOIN users
                ON users.id = updated.user_id
            """,
            workspace_id,
            user_id,
            role,
        )

        if row is None:
            return None

        return self._to_member_entity(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        user_id: UUID,
    ) -> UUID | None:
        """Revoke a membership, returning the user id it named, or nothing.

        Scoped by workspace for the reason `update_role` gives. The row is
        deleted rather than flagged: `workspace_members` records who belongs,
        and a membership that is over is a row that is gone -- 004 gives it no
        column that could say otherwise.
        """
        removed = await connection.fetchval(
            """
            DELETE FROM workspace_members
            WHERE workspace_id = $1 AND user_id = $2
            RETURNING user_id
            """,
            workspace_id,
            user_id,
        )

        if removed is None:
            return None

        # Annotated rather than returned inline: asyncpg ships no types, so
        # `fetchval` is Any and would silently satisfy any return type.
        removed_id: UUID = removed

        return removed_id

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> WorkspaceMembershipEntity:
        return WorkspaceMembershipEntity(
            workspace_id=row["workspace_id"],
            workspace_slug=row["workspace_slug"],
            workspace_name=row["workspace_name"],
            user_id=row["user_id"],
            role=row["role"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _to_member_entity(row: asyncpg.Record) -> WorkspaceMemberEntity:
        return WorkspaceMemberEntity(
            user_id=row["user_id"],
            email=row["email"],
            name=row["name"],
            role=row["role"],
            created_at=row["created_at"],
        )
