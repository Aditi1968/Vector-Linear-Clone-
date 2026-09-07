from collections.abc import Sequence
from uuid import UUID

import asyncpg

from app.domain.activity import IssueSnapshot
from app.domain.bulk import BulkIssuePatch
from app.domain.issues import TERMINAL_STATE_CATEGORIES, UNSET, IssueEntity, Unset
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import ISSUE_COLUMNS


def _value[T](field: T | Unset) -> T | None:
    """The value a patch field carries, with UNSET flattened to None.

    A second copy of the helper in `app/repositories/issues.py`, and the same
    contract: every use is paired with a separate `is not UNSET` flag in the
    same statement, so the None returned for an absent field is never read --
    the CASE it feeds takes its ELSE branch. Flattening rather than raising
    keeps the two parameters of a field independent, which is what lets one
    uniform pair serve a field whose real value may itself be None.
    """
    if isinstance(field, Unset):
        return None

    return field


def _issue_entity(row: asyncpg.Record) -> IssueEntity:
    """One issue row as a domain entity.

    A second copy of `IssueRepository._to_entity`, for the reason
    `app/repositories/relations.py` gives about its own: that method is private
    to the class that owns `issues` reads, and reaching into it would make a
    change there silently a change here. `ISSUE_COLUMNS` is imported rather
    than copied, because it is public and because the SELECT and this mapping
    must agree column for column.
    """
    return IssueEntity(
        id=row["id"],
        team_id=row["team_id"],
        team_key=row["team_key"],
        number=row["number"],
        title=row["title"],
        description=row["description"],
        priority=row["priority"],
        workflow_state_id=row["workflow_state_id"],
        assignee_id=row["assignee_id"],
        creator_id=row["creator_id"],
        estimate=row["estimate"],
        due_date=row["due_date"],
        cycle_id=row["cycle_id"],
        project_id=row["project_id"],
        milestone_id=row["milestone_id"],
        completed_at=row["completed_at"],
        archived_at=row["archived_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class BulkRepository:
    """Set-based writes over `issues`: one statement, many rows, one answer.

    A third repository over `issues`, beside `IssueRepository` and
    `RelationRepository`, and the split is by the shape of the write rather
    than by the table. Every statement here takes an ARRAY of ids and returns
    what it actually touched, because that count is the only thing standing
    between a bulk mutation and a partial one -- `UPDATE ... WHERE id = ANY()`
    reports success for a batch in which half the ids named nothing, so the
    caller has to be told how many rows there were.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    THE TENANCY RULE, stated once for the whole class. Every statement carries
    `workspace_id = $1` ANDed onto the id array, never folded into it. So an id
    from another workspace does not select that workspace's row: it selects
    nothing, and is therefore missing from the RETURNING. The service compares
    the count and fails the whole batch, which is what turns "this id is not
    yours" into "nothing happened" rather than into "everything but that one
    happened". A statement here that took the ids first and checked the tenant
    afterwards would have already read the other tenant's rows.

    `= ANY($n::UUID[])` and never an IN list built by string formatting -- one
    parameter carrying an array, so the statement text is fixed whatever the
    batch size and asyncpg keeps one prepared statement for it.
    """

    async def lock_snapshots(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
    ) -> dict[UUID, IssueSnapshot]:
        """The fields activity reports on for every live issue among these ids.

        Three jobs in one statement, which is why it is the first thing every
        method in `BulkService` runs.

        It is the AUTHORISATION probe. The keys of the returned dict are
        exactly the ids that exist in this workspace and are not archived, so
        the caller learns how many of the ids it was handed are real without
        learning which -- and, crucially, without a second round trip in which
        the answer could change.

        It is the HISTORY read. `IssueRepository.lock_snapshot` explains why
        the before-values have to be read under the lock that the write will
        hold: without it another transaction may commit in between, and the
        timeline would record "from A to C" for a row that went A to B to C --
        a wrong claim about the past, which is worse than a missing one.

        And it is the DEADLOCK guard, which is the one job the single-issue
        version does not have. `UPDATE ... WHERE id = ANY(...)` locks rows in
        whatever order the plan produces, so two concurrent bulk actions over
        overlapping selections can each hold a row the other wants. `ORDER BY
        id` here fixes a total order that every batch takes its locks in, so
        the second one blocks instead of deadlocking. It works only because
        every write path in `BulkService` runs this first; a future method that
        skipped it would reintroduce the deadlock with nothing failing to say
        so.

        Keyed by id rather than returned as a list, because every caller needs
        to look one up while iterating the rows the write returned, and
        building that mapping in the service would be the same dict written one
        layer further from the query that produced it.
        """
        rows = await connection.fetch(
            """
            SELECT
                id,
                title,
                priority,
                workflow_state_id,
                assignee_id,
                project_id,
                cycle_id
            FROM issues
            WHERE workspace_id = $1
                AND id = ANY($2::UUID[])
                AND archived_at IS NULL
            ORDER BY id
            FOR UPDATE
            """,
            scope.workspace_id,
            list(issue_ids),
        )

        return {
            row["id"]: IssueSnapshot(
                title=row["title"],
                priority=row["priority"],
                workflow_state_id=row["workflow_state_id"],
                assignee_id=row["assignee_id"],
                project_id=row["project_id"],
                cycle_id=row["cycle_id"],
            )
            for row in rows
        }

    async def update_many(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
        patch: BulkIssuePatch,
    ) -> list[IssueEntity]:
        """Apply one patch to every live issue among these ids in this workspace.

        The same statement `IssueRepository.update` issues, with an array where
        it has an id and three more columns in the SET list. Every argument
        that method makes for its shape applies here and is not repeated:
        `CASE WHEN <flag> THEN <value> ELSE <column> END` per field, two
        parameters rather than one, so "set this to nothing" and "do not touch
        this" stay distinct; the whole merge evaluated by the server while it
        holds the rows, so nothing is read into this process and written back
        stale.

        What is new is what a bulk write can be refused for, and it is worth
        naming because each refusal takes the WHOLE batch down:

          * a workflow state belonging to one team, applied to a selection
            spanning several. `issues_workflow_state_fk` is
            (workspace_id, team_id, workflow_state_id), so the statement is
            refused for the issues on other teams -- and therefore for all of
            them. That is the correct answer: "move these to In Progress" over
            a mixed selection names a state most of them cannot be in, and
            moving the ones that can would be a different request.
          * an assignee who is not a member of this workspace, or a project,
            milestone or cycle from another tenant. Each is a composite foreign
            key and each is refused as part of this statement, against the
            issue's own committed team and workspace rather than against
            whatever a preceding SELECT read.

        `completed_at` is recomputed on every row rather than only where the
        state moved, and from the state the row ENDS UP in. That is what makes
        the rule self-repairing -- a row whose `completed_at` disagrees with
        its state, however it got that way, is corrected by the next write --
        and it is the same expression the single-issue path uses, so the two
        cannot drift.

        The workflow-state subquery is scoped to `$1` for the reason
        `IssueRepository.update` gives: a state id from another tenant would
        otherwise be READ here to decide `completed_at`, and a statement that
        reads another tenant's row is not one to leave in place because its
        result happens to be discarded.

        `archived_at IS NULL` appears in both the CTE and the outer predicate,
        matching the single-issue path. An archived issue is not there to edit,
        and it is absent from the result rather than reported -- which is the
        same answer an id from another workspace gets, so a caller cannot use a
        bulk action to learn that an id it guessed is real but archived.
        """
        rows = await connection.fetch(
            f"""
            WITH target AS (
                SELECT
                    issues.id,
                    CASE
                        WHEN $3 THEN $4::UUID
                        ELSE issues.workflow_state_id
                    END AS workflow_state_id
                FROM issues
                WHERE issues.workspace_id = $1
                    AND issues.id = ANY($2::UUID[])
                    AND issues.archived_at IS NULL
            )
            UPDATE issues
            SET
                workflow_state_id = target.workflow_state_id,
                assignee_id = CASE
                    WHEN $5 THEN $6::UUID
                    ELSE issues.assignee_id
                END,
                priority = CASE
                    WHEN $7 THEN $8::SMALLINT
                    ELSE issues.priority
                END,
                estimate = CASE
                    WHEN $9 THEN $10::INTEGER
                    ELSE issues.estimate
                END,
                due_date = CASE
                    WHEN $11 THEN $12::DATE
                    ELSE issues.due_date
                END,
                project_id = CASE
                    WHEN $13 THEN $14::UUID
                    ELSE issues.project_id
                END,
                milestone_id = CASE
                    WHEN $15 THEN $16::UUID
                    ELSE issues.milestone_id
                END,
                cycle_id = CASE
                    WHEN $17 THEN $18::UUID
                    ELSE issues.cycle_id
                END,
                completed_at = CASE
                    WHEN (
                        SELECT workflow_states.type
                        FROM workflow_states
                        WHERE workflow_states.id = target.workflow_state_id
                            AND workflow_states.workspace_id = $1
                    ) = ANY($19::TEXT[])
                    THEN COALESCE(issues.completed_at, now())
                    ELSE NULL
                END,
                updated_at = now()
            FROM target
            WHERE issues.workspace_id = $1
                AND issues.id = target.id
                AND issues.archived_at IS NULL
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            list(issue_ids),
            patch.workflow_state_id is not UNSET,
            _value(patch.workflow_state_id),
            patch.assignee_id is not UNSET,
            _value(patch.assignee_id),
            patch.priority is not UNSET,
            _value(patch.priority),
            patch.estimate is not UNSET,
            _value(patch.estimate),
            patch.due_date is not UNSET,
            _value(patch.due_date),
            patch.project_id is not UNSET,
            _value(patch.project_id),
            patch.milestone_id is not UNSET,
            _value(patch.milestone_id),
            patch.cycle_id is not UNSET,
            _value(patch.cycle_id),
            list(TERMINAL_STATE_CATEGORIES),
        )

        return [_issue_entity(row) for row in rows]

    async def touch_many(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
    ) -> list[IssueEntity]:
        """Re-read every live issue among these ids, in this workspace.

        For the bulk action that changed no column of `issues` -- adding or
        removing labels -- where the payload still has to carry the issues so a
        client can re-select `labels` on them. `update_many` with an empty patch
        would do the same reading and would additionally stamp `updated_at` on
        every row, recording an edit that changed nothing.

        Deliberately not `IssueRepository.find_many_by_ids`, though the two
        answer nearly the same question. That method is the batch loader behind
        a DataLoader and belongs to whoever tunes it; this one is part of a
        transaction that has already locked these rows, and the SELECT is a
        read of state this transaction produced rather than of state it found.
        """
        rows = await connection.fetch(
            f"""
            SELECT
{ISSUE_COLUMNS}
            FROM issues
            WHERE issues.workspace_id = $1
                AND issues.id = ANY($2::UUID[])
                AND issues.archived_at IS NULL
            """,
            scope.workspace_id,
            list(issue_ids),
        )

        return [_issue_entity(row) for row in rows]

    async def archive_many(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
    ) -> list[IssueEntity]:
        """Take every named issue off this workspace's board.

        `archived_at IS NULL` in the predicate means an already-archived issue
        is absent from the result rather than archived again, which keeps the
        timestamp meaning "when this was archived". The caller counts, so that
        absence fails the whole batch -- and a client that selected a list it
        had already archived half of is told to refresh rather than being given
        a success that moved nothing.

        `issues_triage_is_not_archived` is what refuses an issue still waiting
        in a triage queue, and it refuses it as a CheckViolationError that
        `BulkService` translates. Archiving unaccepted work would empty a
        team's queue without anybody deciding anything, and migration 021 makes
        that a refusal rather than a silent side effect.

        The rows are returned rather than a count. An archive is a state change
        the client has to render, and this is the last time any read in the
        product will hand these rows back.
        """
        rows = await connection.fetch(
            f"""
            UPDATE issues
            SET archived_at = now(),
                updated_at = now()
            WHERE workspace_id = $1
                AND id = ANY($2::UUID[])
                AND archived_at IS NULL
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            list(issue_ids),
        )

        return [_issue_entity(row) for row in rows]
