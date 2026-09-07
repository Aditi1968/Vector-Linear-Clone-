from uuid import UUID

import asyncpg

from app.domain.labels import LabelGroupEntity
from app.domain.tenancy import WorkspaceScope


class LabelGroupRepository:
    """SQL access for `label_groups`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace and the scope arrives as a
    required keyword argument, for the reasons `LabelRepository` spells out: a
    caller cannot reach this class without having decided which tenant it
    addresses, and `scope=` appears literally at every call site.

    Nothing here translates a constraint violation.
    `label_groups_workspace_name_key` is a duplicate name and is a client's to
    correct; `issue_labels_exclusive_group_key`, raised two tables away when
    `update` flips `exclusive`, means a workspace's own data already breaks the
    rule it is being asked to impose. Telling those apart is a business rule,
    so it lives in the service and this class lets asyncpg raise.
    """

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        group_id: UUID,
    ) -> LabelGroupEntity | None:
        """The group with this id in this workspace, or nothing.

        The workspace is part of the lookup rather than a check applied
        afterwards, so a group belonging to another tenant answers exactly as
        an id that exists nowhere.
        """
        row = await connection.fetchrow(
            """
            SELECT
                id,
                name,
                exclusive,
                created_at,
                updated_at
            FROM label_groups
            WHERE workspace_id = $1 AND id = $2
            """,
            scope.workspace_id,
            group_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        name: str,
        exclusive: bool,
    ) -> LabelGroupEntity:
        """Insert one group into this workspace.

        `workspace_id` is written explicitly and carries no database default,
        so a statement that forgot the tenant is a NOT NULL violation rather
        than a row filed under whichever workspace a default named.

        `exclusive` is written explicitly for the same reason and a second one:
        migration 021 gives the column no default, because the two answers are
        not interchangeable -- an exclusive group refuses writes a plain one
        accepts -- and a default would decide that silently for every group
        nobody thought about.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO label_groups (workspace_id, name, exclusive)
            VALUES ($1, $2, $3)
            RETURNING
                id,
                name,
                exclusive,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            name,
            exclusive,
        )

        return self._to_entity(row)

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        group_id: UUID,
        name: str,
        exclusive: bool,
    ) -> LabelGroupEntity | None:
        """Replace this group's editable fields, or answer nothing.

        Writing `exclusive` here is the whole reason migration 021 spends two
        ON UPDATE CASCADE clauses on it, and the chain is worth knowing about
        from this side. Flipping the flag rewrites `labels.group_exclusive` for
        this group's labels, which recomputes each one's generated
        `exclusivity_key`, which rewrites `issue_labels.exclusivity_key` for
        every association wearing them -- and if any issue in the workspace is
        already wearing two labels from this group, that last hop violates
        `issue_labels_exclusive_group_key` and this statement fails.

        That failure is the point, and it is why nothing here counts first. A
        `SELECT ... GROUP BY issue_id HAVING count(*) > 1` before the update
        would be the same rule stated a second time, weaker, with a round trip
        in the middle during which another request can attach the label that
        breaks it. The service translates the violation; it does not predict
        it.

        The workspace is in the WHERE clause, so an id belonging to another
        tenant updates no rows and returns None -- the same answer as an id
        that exists nowhere, reached without ever reading the other tenant's
        row.

        `updated_at` is assigned in the statement because this schema has no
        touch trigger; see the head of migrations/007_labels_comments.sql for
        why it has none.
        """
        row = await connection.fetchrow(
            """
            UPDATE label_groups
            SET name = $3,
                exclusive = $4,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
                id,
                name,
                exclusive,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            group_id,
            name,
            exclusive,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        group_id: UUID,
    ) -> bool:
        """Remove this group from this workspace; True if a row went.

        `labels_group_fk` is ON DELETE RESTRICT, so this refuses while any
        label is still in the group. That is deliberate -- migration 021
        declines to choose between ungrouping the labels and deleting them --
        and `LabelService.delete_group` ungroups them itself, first, in the
        same transaction.

        RETURNING rather than the command tag: a tag has to be parsed out of a
        string, and a boolean is what the caller actually asked for.
        """
        deleted = await connection.fetchval(
            """
            DELETE FROM label_groups
            WHERE workspace_id = $1 AND id = $2
            RETURNING id
            """,
            scope.workspace_id,
            group_id,
        )

        return deleted is not None

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        limit: int,
    ) -> list[LabelGroupEntity]:
        """One workspace's groups, alphabetically.

        Unpaginated, and bounded by `limit` instead. A workspace has a handful
        of label groups -- they are the axes a team classifies work along, not
        the classifications themselves -- so a cursor would be machinery for a
        list that fits on a screen. The bound is still here because "a handful"
        is a expectation and not a constraint: without it one workspace could
        make this read unbounded, and `LabelGroupConnection` would be an
        unpaginated field the complexity rule charges as one.

        `ORDER BY name, id` on the raw column, served by
        `label_groups_workspace_name_idx`. The unique index beside it is over
        `lower(name)` and can only answer a query written in terms of
        `lower(name)`, which this deliberately is not -- 007 makes the same
        distinction for labels.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                name,
                exclusive,
                created_at,
                updated_at
            FROM label_groups
            WHERE workspace_id = $1
            ORDER BY name, id
            LIMIT $2
            """,
            scope.workspace_id,
            limit,
        )

        return [self._to_entity(row) for row in rows]

    async def ungroup_labels(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        group_id: UUID,
    ) -> None:
        """Take every label out of this group, leaving the labels alone.

        Called on the way to deleting the group, in the same transaction, and
        it is what makes `labels_group_fk`'s RESTRICT a guard on ordering
        rather than an obstacle to it.

        Both columns are cleared together because `labels_group_exclusive_paired`
        refuses a row where one is NULL and the other is not -- so clearing
        only `group_id` is a state the server would reject, not merely one that
        would look odd. `IssueRepository.clear_project` makes the same argument
        about `project_id` and `milestone_id`.

        Clearing them also recomputes each label's generated `exclusivity_key`
        back to its own id, which cascades into `issue_labels` and removes
        those associations from the exclusivity index. Nothing is lost: every
        issue keeps every label it was wearing, and only the rule about wearing
        them goes.
        """
        await connection.execute(
            """
            UPDATE labels
            SET group_id = NULL,
                group_exclusive = NULL,
                updated_at = now()
            WHERE workspace_id = $1 AND group_id = $2
            """,
            scope.workspace_id,
            group_id,
        )

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> LabelGroupEntity:
        return LabelGroupEntity(
            id=row["id"],
            name=row["name"],
            exclusive=row["exclusive"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
