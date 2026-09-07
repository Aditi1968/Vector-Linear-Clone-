from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.issues import TERMINAL_STATE_CATEGORIES, IssueEntity
from app.domain.tenancy import WorkspaceScope
from app.domain.triage import TriageIssueEntity
from app.repositories.issues import ISSUE_COLUMNS


# The workflow-state category an issue lands in when triage declines it, and
# when it is marked a duplicate.
#
# A category and never a name. 'Canceled' is a label a team may rename or
# delete; 'canceled' is what migration 005's `workflow_states_type_check`
# constrains, and comparing categories is the discipline every other state
# decision in this codebase follows -- see the note on `type` at the head of
# 005 and `TERMINAL_STATE_CATEGORIES` in the domain.
#
# 'canceled' with one L throughout, matching schema, domain and API. Two
# spellings of it in one system is a CHECK violation on a value that looks
# correct to whoever wrote it.
DECLINED_CATEGORY = "canceled"


def _issue_entity(row: asyncpg.Record) -> IssueEntity:
    """One issue row as a domain entity.

    A second copy of `IssueRepository._to_entity` rather than an import of it,
    for the reason `app/repositories/relations.py` gives about its own copy:
    that method is private to the class that owns `issues` reads, and reaching
    into it would make a change there silently a change here.

    `ISSUE_COLUMNS` is imported rather than copied, because it is public and
    because the two must select the same columns -- a column added to the
    entity and missing from the SELECT is a KeyError at runtime, on whichever
    triage path happens not to be exercised. The column list and the mapping
    are two different risks: one copy of the list is safe to share, one copy of
    the mapping would couple this file to a private method.
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


class TriageRepository:
    """SQL access for the triage queue, which lives on `issues`.

    A second repository over a table another one owns, exactly as
    `RelationRepository` is: that class writes `issues.parent_id` and reads
    sub-issues, this one writes `issues.triage_entered_at` and reads the queue.
    The alternative -- every statement about `issues` in
    `app/repositories/issues.py` -- is the argument that a table gets one file,
    and it loses here for the reason it lost there: a feature's statements
    belong with the feature, and `IssueRepository` is already the largest file
    in this layer.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace and the scope arrives as a
    required keyword argument, so "does this query cross tenants" is answered
    by reading the call site.

    Every write carries `archived_at IS NULL`. Migration 021 also declares
    `issues_triage_is_not_archived`, so an archived issue cannot be in a queue
    at all -- these predicates are what keep an archived issue from being put
    into one, which the constraint would refuse with a message about a CHECK
    rather than with the ordinary "no such issue".
    """

    async def enter(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """Put one issue into its own team's queue, or answer nothing.

        The team is the issue's OWN `team_id` and is not a parameter. A queue
        is per team and an issue belongs to exactly one, so a caller able to
        name the team would be able to file work into a queue the issue is not
        on -- and `triageChangeTeam` is the operation for moving it, which
        renumbers the issue rather than pretending it did not move.

        `triage_entered_at IS NULL` in the predicate makes this match nothing
        rather than succeed on an issue already in the queue, which keeps the
        timestamp meaning "when this arrived" instead of "when somebody last
        pressed the button". The caller cannot tell that case from a
        nonexistent id, another tenant's, or an archived one -- all four are
        None -- and that is correct rather than merely convenient: an issue a
        caller cannot see must not be discoverable by trying to triage it.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues
            SET triage_entered_at = now(),
                updated_at = now()
            WHERE workspace_id = $1
                AND id = $2
                AND archived_at IS NULL
                AND triage_entered_at IS NULL
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
        )

        if row is None:
            return None

        return _issue_entity(row)

    async def accept(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        workflow_state_id: UUID,
    ) -> IssueEntity | None:
        """Take one issue out of triage and into a state somebody chose.

        The state is a parameter because accepting IS choosing one: an issue in
        triage has a `workflow_state_id` only because migration 005 made the
        column NOT NULL, and nobody picked the value it holds. This is the
        write that makes it mean something.

        Nothing checks the state against the issue's team.
        `issues_workflow_state_fk` references
        `workflow_states (workspace_id, team_id, id)` using the issue's OWN
        stored workspace and team, so a state from another team -- or another
        tenant -- is refused by the server as part of this statement, and
        refused against the issue's committed team rather than against whatever
        a preceding SELECT read. `IssueRepository.set_cycle` makes the same
        argument at greater length.

        `completed_at` is recomputed from the state the row ends up in, using
        the same expression `IssueRepository.update` uses and for the same
        reason: the rule -- non-NULL exactly when the state is terminal, and
        keeping the instant already there when moving between two terminal
        categories -- lives in one expression evaluated by the server, so there
        is no branch here for a caller to reach around. An issue accepted
        straight into Done gets a `completed_at`; one accepted into Todo does
        not.

        The subquery reading `workflow_states` is scoped to `$1`. A state id
        from another tenant would otherwise be READ here to decide
        `completed_at` -- the write would still be refused by the foreign key,
        so nothing would be corrupted, but a statement that reads another
        tenant's row is not one to leave in place because its result happens to
        be discarded.

        `triage_entered_at IS NOT NULL` is what makes this an operation on the
        QUEUE rather than a second way to set a workflow state. An issue that
        is not waiting cannot be accepted, and the answer is the same None an
        id that exists nowhere gets.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues
            SET workflow_state_id = $3,
                triage_entered_at = NULL,
                completed_at = CASE
                    WHEN (
                        SELECT workflow_states.type
                        FROM workflow_states
                        WHERE workflow_states.id = $3
                            AND workflow_states.workspace_id = $1
                    ) = ANY($4::TEXT[])
                    THEN COALESCE(issues.completed_at, now())
                    ELSE NULL
                END,
                updated_at = now()
            WHERE workspace_id = $1
                AND id = $2
                AND archived_at IS NULL
                AND triage_entered_at IS NOT NULL
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
            workflow_state_id,
            list(TERMINAL_STATE_CATEGORIES),
        )

        if row is None:
            return None

        return _issue_entity(row)

    async def decline(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """Take one issue out of triage, refused, onto its team's canceled state.

        There is no `workflow_state_id` parameter, and that absence is the
        design. Declining is the decision that this work will not be done, and
        the state it lands in is a consequence of that decision rather than a
        second choice -- a caller able to name it could decline an issue into
        `In Progress`, which would be a refusal the board reports as work in
        flight.

        The state is resolved by a subquery against the issue's OWN team, in
        the same statement, so nothing has to be read first and no team id
        arrives from a client. `ORDER BY position, id LIMIT 1` picks one when a
        team has several canceled states: 005 declines to make `position`
        unique per team, so `id` is what makes the choice total and therefore
        reproducible.

        `completed_at` is stamped rather than recomputed, because the category
        this lands in is terminal by construction -- there is no branch to
        take. `COALESCE` preserves an instant already there, which is the same
        refinement the general rule makes for a move between two terminal
        categories: the work stopped when it first stopped, and relabelling why
        does not restart it.

        A team with no canceled state at all makes the subquery NULL, which is
        a NOT NULL violation on `workflow_state_id` rather than a row with no
        status. That is the right failure and `TriageService` translates it: a
        workspace that deleted its canceled states has a configuration problem,
        not a client with bad input, but the caller still has to be told
        something it can act on.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues
            SET workflow_state_id = (
                    SELECT workflow_states.id
                    FROM workflow_states
                    WHERE workflow_states.workspace_id = issues.workspace_id
                        AND workflow_states.team_id = issues.team_id
                        AND workflow_states.type = $3
                    ORDER BY workflow_states.position, workflow_states.id
                    LIMIT 1
                ),
                triage_entered_at = NULL,
                completed_at = COALESCE(issues.completed_at, now()),
                updated_at = now()
            WHERE workspace_id = $1
                AND id = $2
                AND archived_at IS NULL
                AND triage_entered_at IS NOT NULL
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
            DECLINED_CATEGORY,
        )

        if row is None:
            return None

        return _issue_entity(row)

    async def change_team(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        team_id: UUID,
        number: int,
        workflow_state_id: UUID,
    ) -> IssueEntity | None:
        """Move one queued issue to another team of the same workspace.

        Three columns move together, and none of them can move alone.
        `issues_team_number_key` is UNIQUE (team_id, number), so an issue
        arriving on a team whose number it already carries collides with
        whatever holds it there; `issues_workflow_state_fk` is
        (workspace_id, team_id, workflow_state_id), so the state it was sitting
        in belongs to the team it is leaving. The number and the state are
        therefore parameters, allocated and resolved by `TriageService` from
        the TARGET team inside this transaction.

        The consequence is that the issue's identifier changes -- ENG-9 becomes
        DES-4 -- which everything else in this codebase goes out of its way to
        prevent: 006 archives rather than deletes precisely because a number is
        never reissued and the identifier is the issue's name in URLs, commit
        messages and conversation.

        `triage_entered_at IS NOT NULL` is what makes that acceptable, and it
        is the reason this method is here rather than on `IssueRepository`. An
        issue in triage has not been accepted by anybody: nothing has been
        linked to it, nobody has quoted it, and the identifier it was filed
        with was a guess made by whatever filed it. Renumbering it is
        correcting that guess. Renumbering an issue somebody is working on is a
        different operation with a different answer, and this predicate is what
        keeps the two from being the same method.

        The issue stays IN the queue. Moving work to the right team is not
        deciding what to do with it, and the receiving team is the one that has
        to make that decision -- so the row leaves one team's queue and joins
        another's, which is what a per-team read of one column already means.

        The target team is not checked against the workspace here.
        `issues_team_fk` references `teams (workspace_id, id)` against the
        issue's own stored workspace, so a team from another tenant is refused
        by the server as part of this statement.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues
            SET team_id = $3,
                number = $4,
                workflow_state_id = $5,
                updated_at = now()
            WHERE workspace_id = $1
                AND id = $2
                AND archived_at IS NULL
                AND triage_entered_at IS NOT NULL
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
            team_id,
            number,
            workflow_state_id,
        )

        if row is None:
            return None

        return _issue_entity(row)

    async def list_queue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        limit: int,
        after_entered_at: datetime | None,
        after_id: UUID | None,
    ) -> list[TriageIssueEntity]:
        """Keyset page of one team's queue, oldest first.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison, so
        the cost of page N is the cost of page 1.

        Ascending, unlike every other list in this product. A queue is read in
        arrival order -- the thing that has waited longest is the thing
        somebody has to look at -- where an issue list is read newest first.
        `id` breaks a tie so the ordering is total, which is what keeps a page
        walk from repeating or skipping a row when two issues enter triage in
        the same microsecond, as a bulk import makes them do constantly.

        The tenant predicate leads and the team predicate follows it, both as
        equalities ANDed onto the keyset rather than folded into it. Widening
        the row-value comparison to `(workspace_id, team_id, entered_at, id) >
        (...)` would put workspaces and teams into the ordering, which is how a
        page walk falls out of one queue and into whichever one sorts next.

        `triage_entered_at IS NOT NULL` is spelled literally rather than
        implied by the comparison, because it is the predicate of the partial
        index `issues_workspace_team_triage_idx` and the planner has to see it
        to use it. On the first page the keyset is absent entirely and it is
        the only thing standing between this read and a scan of every issue the
        workspace has ever had.
        """
        if after_entered_at is None or after_id is None:
            rows = await connection.fetch(
                f"""
                SELECT
{ISSUE_COLUMNS},
                    issues.triage_entered_at
                FROM issues
                WHERE issues.workspace_id = $1
                    AND issues.team_id = $2
                    AND issues.archived_at IS NULL
                    AND issues.triage_entered_at IS NOT NULL
                ORDER BY issues.triage_entered_at, issues.id
                LIMIT $3
                """,
                scope.workspace_id,
                team_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                f"""
                SELECT
{ISSUE_COLUMNS},
                    issues.triage_entered_at
                FROM issues
                WHERE issues.workspace_id = $1
                    AND issues.team_id = $2
                    AND issues.archived_at IS NULL
                    AND issues.triage_entered_at IS NOT NULL
                    AND (issues.triage_entered_at, issues.id) > ($3, $4)
                ORDER BY issues.triage_entered_at, issues.id
                LIMIT $5
                """,
                scope.workspace_id,
                team_id,
                after_entered_at,
                after_id,
                limit,
            )

        return [
            TriageIssueEntity(
                issue=_issue_entity(row),
                entered_at=row["triage_entered_at"],
            )
            for row in rows
        ]

    async def count_queue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
    ) -> int:
        """How many issues are waiting in this team's queue.

        A number the product actually renders -- the badge next to Triage in a
        team's sidebar -- rather than a decoration, which is the test
        `IssueRepository.count` sets for whether a second aggregate is worth
        paying for. It is a resolver of its own on the connection, so a
        document that does not select it does not run this.

        Served entirely by `issues_workspace_team_triage_idx`, whose leading
        columns are the two equalities and whose predicate is the third.

        `count(*)` and not `count(id)`: identical here, since `id` is NOT NULL,
        and the star form is the one the planner special-cases.
        """
        total: int = await connection.fetchval(
            """
            SELECT count(*)
            FROM issues
            WHERE workspace_id = $1
                AND team_id = $2
                AND archived_at IS NULL
                AND triage_entered_at IS NOT NULL
            """,
            scope.workspace_id,
            team_id,
        )

        return total
