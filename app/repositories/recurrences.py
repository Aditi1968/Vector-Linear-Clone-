from datetime import date
from uuid import UUID

import asyncpg

from app.domain.recurrence import (
    RecurrenceEntity,
    RecurrenceFrequency,
    RecurrenceRule,
)
from app.domain.tenancy import WorkspaceScope


# Every column a RecurrenceEntity is built from, as one expression list shared
# by the statements that return one.
#
# Interpolated from a module-level literal that no input can influence, which
# is the same defence `ISSUE_COLUMNS` and `_TEMPLATE_COLUMNS` carry:
# "parameterized SQL only" is a rule about VALUES, and the alternative here is
# four copies of the same ten lines that drift the day a column joins the
# entity. Every actual value below arrives as $n.
RECURRENCE_COLUMNS = """
                recurrence.template_id,
                recurrence.team_id,
                recurrence.frequency,
                recurrence.interval_count,
                recurrence.weekdays,
                recurrence.day_of_month,
                recurrence.starts_on,
                recurrence.due_in_days,
                recurrence.next_run_on
"""


class RecurrenceRepository:
    """SQL access for `issue_recurrences`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a transaction.
    `asyncpg.Record` never escapes this class.

    TWO KINDS OF STATEMENT, WITH TWO TENANCY SHAPES, and the difference is
    worth stating rather than leaving to look inconsistent.

    The CRUD half -- `upsert`, `delete` -- is issued on behalf of a person
    editing a template, and every one of them carries `workspace_id = $1` from
    an `AuthorizedWorkspaceScope`. A template id from another tenant matches no
    row and answers nothing, exactly as `TemplateRepository.get` does.

    The SWEEP half -- `lock_due`, `advance` -- carries no workspace predicate on
    the way in, deliberately: a background pass is the one reader in this system
    with no tenant, and its whole job is to work through every workspace's due
    schedules. Nothing it returns reaches a client. The rows are consumed by
    `ScheduleWorker`, which files each issue under the `workspace_id` READ OFF
    THE ROW -- never one a caller supplied -- and migration 029's composite
    foreign keys are what make that read trustworthy: there is one
    `workspace_id` column on the row, and the template reference and the team
    reference both read it, so a schedule pairing one tenant's template with
    another's team is not a row that exists to be claimed. The same argument
    `EmbeddingJobRepository` makes in full.
    """

    async def upsert(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
        team_id: UUID,
        rule: RecurrenceRule,
        next_run_on: date,
    ) -> RecurrenceEntity | None:
        """Save this template's schedule, replacing whatever it had.

        An upsert and not a create/update pair, because there is at most one
        schedule per template -- `issue_recurrences_pkey` says so -- and the
        product operation is "this template recurs like THIS", which is the same
        request whether or not it recurred before. Two mutations differing only
        in whether a row happens to exist would put that question on the client.

        `next_run_on` is computed by the SERVICE and passed in rather than
        derived here, because deriving it is calendar arithmetic and this class
        holds SQL. `app.domain.recurrence.first_occurrence` is the one place
        that decides when a rule first fires, so a save and a sweep cannot
        disagree about it.

        THE UPDATE RESETS `next_run_on`, which is the right answer and worth
        saying: editing a schedule means the next occurrence is the first one
        the NEW rule names, not whatever the old one had queued. A monthly
        recurrence changed to weekly must not wait out the month.

        Nothing checks the team or the template against the workspace first. The
        two composite foreign keys migration 029 declares refuse each mismatch
        as part of this statement, against the workspace the scope carries -- so
        a pre-check would be a second, weaker copy of two rules, weaker because
        a template can be deleted between the two statements. The violation
        reaches `TemplateService`, which decides what a client is told.

        None means the write matched nothing, which under an upsert cannot
        happen -- the INSERT either lands or conflicts. It is returned anyway
        so that the read-back below has one shape, and asserted by the caller.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO issue_recurrences AS recurrence (
                workspace_id,
                template_id,
                team_id,
                frequency,
                interval_count,
                weekdays,
                day_of_month,
                starts_on,
                due_in_days,
                next_run_on
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT ON CONSTRAINT issue_recurrences_pkey DO UPDATE
            SET team_id = EXCLUDED.team_id,
                frequency = EXCLUDED.frequency,
                interval_count = EXCLUDED.interval_count,
                weekdays = EXCLUDED.weekdays,
                day_of_month = EXCLUDED.day_of_month,
                starts_on = EXCLUDED.starts_on,
                due_in_days = EXCLUDED.due_in_days,
                next_run_on = EXCLUDED.next_run_on,
                updated_at = now()
            RETURNING
{RECURRENCE_COLUMNS}
            """,
            scope.workspace_id,
            template_id,
            team_id,
            rule.frequency.value,
            rule.interval_count,
            # None rather than an empty array for the frequencies that have no
            # weekdays: `issue_recurrences_weekdays_match_frequency` is written
            # as `IS NOT NULL`, and an empty array is not null -- it would be a
            # weekly schedule that names no day and therefore never fires,
            # stored by a constraint that thought it had refused exactly that.
            list(rule.weekdays) or None,
            rule.day_of_month,
            rule.starts_on,
            rule.due_in_days,
            next_run_on,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        template_id: UUID,
    ) -> bool:
        """Stop this template recurring; True if a schedule went.

        Also called on the way to deleting the template itself --
        `issue_recurrences_template_fk` is RESTRICT, so this row goes first, in
        the same transaction, which is the constraint working as a guard on
        that ordering rather than an obstacle to it. See
        `TemplateRepository.delete`, which does the same for the label rows.

        A template in another workspace deletes nothing and answers False,
        indistinguishable from one that has no schedule -- and clearing a
        schedule that was never set is a successful no-op at the service, for
        the reason `ActivityService.unsubscribe` gives.
        """
        deleted = await connection.fetchval(
            """
            DELETE FROM issue_recurrences
            WHERE workspace_id = $1 AND template_id = $2
            RETURNING template_id
            """,
            scope.workspace_id,
            template_id,
        )

        return deleted is not None

    async def lock_due(
        self,
        connection: asyncpg.Connection,
        *,
        limit: int,
    ) -> tuple[date, list[tuple[UUID, RecurrenceEntity]]]:
        """Take the schedules due today, locked, with the day they are due AS OF.

        THE STATEMENT THAT MAKES TWO REPLICAS SAFE, and the whole of it is
        `FOR UPDATE SKIP LOCKED`. Two sweeps running this in the same instant
        take disjoint sets: rows the other has locked are skipped during the
        scan rather than waited on, so neither blocks and neither sees a row the
        other took. Without SKIP LOCKED the second would block on the first's
        lock and then claim the very same rows once it committed, which is the
        classic way a schedule files everything twice.

        The lock is held until the caller's transaction ends, and the caller
        calls `advance` inside it -- so by the time any other process can see
        these rows, `next_run_on` has moved past today and their predicate no
        longer matches. That is what turns a lock into a claim.
        `EventRepository.claim_next` makes the identical argument.

        `CURRENT_DATE` COMES BACK WITH THE ROWS, and that is not a convenience.
        The advance is calendar arithmetic in Python -- weekday sets and month
        clamping are not things to write in SQL -- and the day it computes from
        has to be the day the predicate selected on. Reading the process clock
        instead would let a replica whose container drifted, or one that crossed
        midnight between the statement and the arithmetic, advance a schedule to
        a different day from the one that claimed it.

        The workspace comes back with each row, first in the pair, so the caller
        files an issue under the id the DATABASE matched rather than under one
        anything else supplied.

        Ordered by `next_run_on, workspace_id, template_id`, matching
        `issue_recurrences_due_idx` exactly, so this is a walk of that index
        that stops at today rather than a sort of the table. Oldest-due first,
        so a backlog after an outage is worked in schedule order.
        """
        rows = await connection.fetch(
            f"""
            SELECT
                CURRENT_DATE AS today,
                recurrence.workspace_id,
{RECURRENCE_COLUMNS}
            FROM issue_recurrences AS recurrence
            WHERE recurrence.next_run_on <= CURRENT_DATE
            ORDER BY
                recurrence.next_run_on,
                recurrence.workspace_id,
                recurrence.template_id
            LIMIT $1
            FOR UPDATE SKIP LOCKED
            """,
            limit,
        )

        if not rows:
            # No rows means no `today` to read off one, and the caller has
            # nothing to compute anyway. `date.min` would be a lie in a value
            # the caller might use; an empty list is what it checks first.
            return _NO_DUE_DATE, []

        return rows[0]["today"], [
            (row["workspace_id"], self._to_entity(row)) for row in rows
        ]

    async def advance(
        self,
        connection: asyncpg.Connection,
        *,
        workspace_id: UUID,
        template_id: UUID,
        next_run_on: date,
    ) -> None:
        """Move one claimed schedule on to its next occurrence.

        Called inside the same transaction as `lock_due`, on a row that
        transaction already holds, which is what makes the pair a claim: the
        commit publishes both the lock release and the moved date at once, so no
        other process ever sees a row that is still due and no longer locked.

        `workspace_id` is in the predicate even though `template_id` alone would
        be unique. It is the primary key's leading column, so this is a seek
        rather than an index scan -- and it means a caller that somehow held the
        wrong workspace could not move another tenant's schedule by id. The same
        reasoning `EmbeddingJobRepository.complete` gives.
        """
        await connection.execute(
            """
            UPDATE issue_recurrences
            SET next_run_on = $3,
                updated_at = now()
            WHERE workspace_id = $1 AND template_id = $2
            """,
            workspace_id,
            template_id,
            next_run_on,
        )

    async def find_many_for_templates(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        template_ids: list[UUID],
    ) -> dict[UUID, RecurrenceEntity]:
        """The schedules of several templates, keyed by template, in one trip.

        Batched over the ids rather than queried per template: the caller is
        rendering a template menu, and a query per row is the N+1 that makes a
        menu's cost depend on how many templates a workspace has. The same shape
        `TeamRepository.list_workflow_states` uses for the same reason.

        A dict rather than a list, because the caller has to attach each
        schedule to the template it belongs to and building that mapping from a
        list would be the same loop written at the call site -- where the key
        would be chosen by whoever wrote it rather than by the row.

        A template id from another workspace simply produces no entry, because
        the tenant predicate leads here as everywhere.
        """
        rows = await connection.fetch(
            f"""
            SELECT
{RECURRENCE_COLUMNS}
            FROM issue_recurrences AS recurrence
            WHERE recurrence.workspace_id = $1
                AND recurrence.template_id = ANY($2::UUID[])
            """,
            scope.workspace_id,
            template_ids,
        )

        return {row["template_id"]: self._to_entity(row) for row in rows}

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> RecurrenceEntity:
        return RecurrenceEntity(
            template_id=row["template_id"],
            team_id=row["team_id"],
            # Through the enum rather than passed as a string, so a frequency
            # the domain does not know about fails at the one place that reads
            # it out of a row.
            frequency=RecurrenceFrequency(row["frequency"]),
            interval_count=row["interval_count"],
            # A tuple, and `()` for the NULL the two non-weekly frequencies
            # store: `RecurrenceRule` promises `in` works without a guard, and
            # that promise is kept here rather than at every reader.
            weekdays=tuple(row["weekdays"] or ()),
            day_of_month=row["day_of_month"],
            starts_on=row["starts_on"],
            due_in_days=row["due_in_days"],
            next_run_on=row["next_run_on"],
        )


# What `lock_due` reports as "today" when it found nothing.
#
# `date.min` rather than None, so the return type stays a plain pair and every
# caller does not have to narrow one. It is unusable by construction: the list
# beside it is empty, so there is nothing to compute a next occurrence for, and
# a caller that used it anyway would produce a date before every `starts_on` in
# the schema -- which `occurs_on` answers False for, rather than silently
# filing something.
_NO_DUE_DATE = date.min
