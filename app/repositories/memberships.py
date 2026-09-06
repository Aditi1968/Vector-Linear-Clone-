from uuid import UUID

import asyncpg

from app.domain.memberships import WorkspaceMembershipEntity


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
