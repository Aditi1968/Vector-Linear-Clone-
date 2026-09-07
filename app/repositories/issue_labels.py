from collections import defaultdict
from collections.abc import Sequence
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
    ) -> bool:
        """Apply a label to an issue in this workspace; False if no such label.

        INSERT ... SELECT rather than INSERT ... VALUES, and the difference is
        migration 021's `issue_labels.exclusivity_key`. That column is the
        label's own derived key -- the group's id for a label in an exclusive
        group, the label's id for every other -- and it is what the partial
        unique index enforcing exclusivity is built over. The only correct
        source for it is the label's row, so it is READ in the same statement
        that writes it: there is no round trip during which the label could be
        regrouped, and `issue_labels_exclusivity_fk` checks the carried value
        against the label afterwards regardless.

        The consequence is that an unknown label is no longer a foreign-key
        violation but an empty SELECT, and therefore an insert of zero rows.
        That is why this returns a boolean where it used to return None: False
        means no label with that id exists IN THIS WORKSPACE, which is the same
        answer `issue_labels_label_fk` used to give and is still indistinguishable
        from a label belonging to another tenant.

        Everything else still raises, and each is worth distinguishing:
        `issue_labels_pkey` when the label is already applied,
        `issue_labels_exclusive_group_key` when the issue already wears another
        label from the same exclusive group, `issue_labels_issue_fk` when the
        issue is not this workspace's. The service reads the constraint name to
        decide which are the client's to fix.

        The first two can never be confused for one another, and that is a
        property of the index rather than of this statement: migration 021
        makes the exclusivity index partial on `exclusivity_key <> label_id`,
        which removes every ungrouped association from it, so a duplicate
        attach can only ever be the primary key.

        No ON CONFLICT. An upsert would report success without establishing
        that the existing row is the row this call meant to write, which is
        the same objection migrations/002_tenancy.sql raises to ON CONFLICT in
        seed inserts. Here it would additionally erase the difference between
        "applied it" and "it was already applied", which the caller is asking
        about.
        """
        inserted = await connection.fetchval(
            """
            INSERT INTO issue_labels
                (workspace_id, issue_id, label_id, exclusivity_key)
            SELECT $1, $2, labels.id, labels.exclusivity_key
            FROM labels
            WHERE labels.workspace_id = $1 AND labels.id = $3
            RETURNING label_id
            """,
            scope.workspace_id,
            issue_id,
            label_id,
        )

        return inserted is not None

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
                labels.group_id AS group_id,
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
                    group_id=row["group_id"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )

        # A plain dict on the way out: a defaultdict would manufacture an
        # empty list for any id a caller looked up, which is exactly the
        # "absent means absent" distinction this method is documented to keep.
        return dict(grouped)

    async def attach_many(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
        label_ids: Sequence[UUID],
    ) -> int:
        """Apply every named label to every named issue; how many rows landed.

        The cross product, minus what is already there. Two arrays and one
        statement rather than a loop of `attach`, because a bulk action is one
        act: a hundred issues and three labels is three hundred round trips
        written the other way, all inside one transaction holding one set of
        locks for the duration.

        `exclusivity_key` is read from `labels` exactly as in `attach`, and the
        join to `labels` is also what makes an unknown label id contribute
        nothing rather than raise. That asymmetry with `attach` is deliberate:
        the single-issue path answers "did this one label exist", while the
        bulk path cannot -- with ON CONFLICT DO NOTHING below, a returned count
        short of the cross product means "already attached" or "no such label"
        and there is no way to tell which. `BulkService` therefore checks the
        label ids separately, with `count_labels_in_workspace`, in the same
        transaction and before this runs.

        ON CONFLICT DO NOTHING, which `attach` above refuses and which is right
        here for the reason that refusal gives. A bulk "add this label" over a
        selection that already includes some issues wearing it is an ordinary
        request -- nobody selecting a page has checked which rows already carry
        the label -- so the difference between "applied it" and "it was already
        applied" is not something this caller is asking about. The
        alternative -- failing the batch because one of a hundred issues was
        already labelled -- would make the action unusable on any real
        selection. It is scoped to `issue_labels_pkey` by naming the conflict
        target, so it silences "already applied" and nothing else: an
        exclusive-group conflict still raises, because that index is not the
        target named.

        The exclusivity index therefore still refuses the batch outright, which
        is the correct all-or-nothing behaviour -- "add Urgent to these twelve"
        when three of them already carry Low from the same exclusive group is a
        request that cannot be satisfied, and satisfying nine of it would be
        worse than refusing it.
        """
        inserted = await connection.fetch(
            """
            INSERT INTO issue_labels
                (workspace_id, issue_id, label_id, exclusivity_key)
            SELECT $1, target.issue_id, labels.id, labels.exclusivity_key
            FROM unnest($2::UUID[]) AS target (issue_id)
            CROSS JOIN labels
            WHERE labels.workspace_id = $1 AND labels.id = ANY($3::UUID[])
            ON CONFLICT ON CONSTRAINT issue_labels_pkey DO NOTHING
            RETURNING label_id
            """,
            scope.workspace_id,
            list(issue_ids),
            list(label_ids),
        )

        return len(inserted)

    async def detach_many(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
        label_ids: Sequence[UUID],
    ) -> int:
        """Remove every named label from every named issue; how many rows went.

        A pair that was never joined is not an error here, unlike in `detach`.
        The single-issue path reports it because a client that just asked to
        remove one label needs to know whether it was there; a bulk removal
        over a selection is asking for a state -- "none of these wear Urgent" --
        and reaching it from a selection where half of them already did not is
        success.

        Both ids are matched under this workspace's key, so a pair naming
        another tenant's issue or label deletes nothing.
        """
        deleted = await connection.fetch(
            """
            DELETE FROM issue_labels
            WHERE workspace_id = $1
                AND issue_id = ANY($2::UUID[])
                AND label_id = ANY($3::UUID[])
            RETURNING label_id
            """,
            scope.workspace_id,
            list(issue_ids),
            list(label_ids),
        )

        return len(deleted)

    async def count_labels_in_workspace(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        label_ids: Sequence[UUID],
    ) -> int:
        """How many of these label ids exist in this workspace.

        The authorisation probe behind every bulk label operation, and it is
        deliberately a COUNT rather than a list of the ones that resolved. The
        caller compares it against the number of DISTINCT ids it asked about
        and refuses the whole batch on a disagreement, so it never needs to
        know WHICH id failed -- and cannot report it, which is the property
        that keeps a bulk mutation from being an existence oracle for another
        tenant's label ids.

        `= ANY($2)` rather than an IN list built by string formatting -- one
        parameter carrying an array, so the statement text is fixed whatever
        the batch size.
        """
        count: int = await connection.fetchval(
            """
            SELECT count(*)
            FROM labels
            WHERE workspace_id = $1 AND id = ANY($2::UUID[])
            """,
            scope.workspace_id,
            list(label_ids),
        )

        return count
