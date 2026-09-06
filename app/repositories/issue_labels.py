from collections import defaultdict
from uuid import UUID

import asyncpg

from app.domain.labels import LabelEntity
from app.domain.tenancy import WorkspaceScope


class IssueLabelRepository:
    """SQL access for `issue_labels`, the issue<->label join.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    `attach` writes ONE workspace_id and lets the server decide whether the
    pair is legal. That is not laziness and not an omission: both foreign keys
    on this table are composite and both are pinned to that single column, so
    an issue from workspace A and a label from workspace B have no value of
    workspace_id that satisfies both parents. Checking it here first would be
    a second, weaker copy of the rule -- weaker because it would be a separate
    statement, so either row could be deleted between the check and the
    insert, and weaker because it would then be two places that have to agree.
    See `IssueRepository.create`, which makes the same argument about
    `issues_team_fk`.
    """

    async def attach(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        label_id: UUID,
    ) -> None:
        """Apply a label to an issue in this workspace.

        Returns nothing, and raises on every failure worth distinguishing:
        `issue_labels_pkey` when the label is already applied,
        `issue_labels_issue_fk` when the issue is not this workspace's,
        `issue_labels_label_fk` when the label is not. The service reads the
        constraint name to decide which are the client's to fix.

        No ON CONFLICT. An upsert would report success without establishing
        that the existing row is the row this call meant to write, which is
        the same objection migrations/002_tenancy.sql raises to ON CONFLICT in
        seed inserts. Here it would additionally erase the difference between
        "applied it" and "it was already applied", which the caller is asking
        about.
        """
        await connection.execute(
            """
            INSERT INTO issue_labels (workspace_id, issue_id, label_id)
            VALUES ($1, $2, $3)
            """,
            scope.workspace_id,
            issue_id,
            label_id,
        )

    async def detach(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        label_id: UUID,
    ) -> bool:
        """Remove a label from an issue; True if a row went.

        Both ids are matched under this workspace's key, so a pair naming
        another tenant's issue or label deletes nothing and answers False --
        indistinguishable from a pair that was never joined.
        """
        deleted = await connection.fetchval(
            """
            DELETE FROM issue_labels
            WHERE workspace_id = $1 AND issue_id = $2 AND label_id = $3
            RETURNING label_id
            """,
            scope.workspace_id,
            issue_id,
            label_id,
        )

        return deleted is not None

    async def count_for_issue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> int:
        """How many labels this issue currently wears in this workspace.

        Served by the leading columns of `issue_labels_pkey`, which is why
        that key leads with (workspace_id, issue_id).
        """
        count: int = await connection.fetchval(
            """
            SELECT count(*)
            FROM issue_labels
            WHERE workspace_id = $1 AND issue_id = $2
            """,
            scope.workspace_id,
            issue_id,
        )

        return count

    async def list_for_issues(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: list[UUID],
    ) -> dict[UUID, list[LabelEntity]]:
        """Every issue's labels, for a batch of issues, in one statement.

        Batched because the alternative is one query per issue on a list page,
        which is the N+1 this method exists to prevent; `app/graphql/loaders/`
        holds the DataLoader that calls it.

        The result is a plain dict of domain entities. Grouping happens here
        rather than in the caller because the grouping key -- `issue_id` -- is
        a column of the join row and is not part of `LabelEntity`; returning
        pairs would either put a storage column on the entity or hand the
        caller a Record.

        Issues with no labels are simply absent from the dict rather than
        mapped to an empty list. A caller that must distinguish "no labels"
        from "not asked about" has the key list it passed in; inventing empty
        entries here would claim this method knows which ids were real.

        The join pins the label to the same workspace as the association. That
        is already guaranteed by `issue_labels_label_fk`, and it is written
        anyway: a read that depends on a constraint holding, without saying
        so, is a read that silently starts crossing tenants if the constraint
        is ever relaxed.

        `= ANY($2)` rather than an IN list built by string formatting -- one
        parameter carrying an array, so the statement text is fixed whatever
        the batch size.
        """
        rows = await connection.fetch(
            """
            SELECT
                issue_labels.issue_id AS issue_id,
                labels.id AS id,
                labels.name AS name,
                labels.color AS color,
                labels.created_at AS created_at,
                labels.updated_at AS updated_at
            FROM issue_labels
            JOIN labels
                ON labels.workspace_id = issue_labels.workspace_id
                AND labels.id = issue_labels.label_id
            WHERE issue_labels.workspace_id = $1
                AND issue_labels.issue_id = ANY($2::uuid[])
            ORDER BY issue_labels.issue_id, labels.name, labels.id
            """,
            scope.workspace_id,
            issue_ids,
        )

        grouped: dict[UUID, list[LabelEntity]] = defaultdict(list)

        for row in rows:
            grouped[row["issue_id"]].append(
                LabelEntity(
                    id=row["id"],
                    name=row["name"],
                    color=row["color"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )

        # A plain dict on the way out: a defaultdict would manufacture an
        # empty list for any id a caller looked up, which is exactly the
        # "absent means absent" distinction this method is documented to keep.
        return dict(grouped)
