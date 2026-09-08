from collections.abc import Sequence
from datetime import date
from uuid import UUID

import asyncpg

from app.domain.activity import IssueSnapshot
from app.domain.estimates import EstimateScale
from app.domain.issues import (
    TERMINAL_STATE_CATEGORIES,
    THIS_WEEK_DAYS,
    UNSET,
    DueWindow,
    IssueEntity,
    IssueFilter,
    IssueOrder,
    IssueOrderField,
    IssuePatch,
    OrderDirection,
    Unset,
)
from app.domain.pagination import IssueListCursor
from app.domain.tenancy import WorkspaceScope


def _value[T](field: T | Unset) -> T | None:
    """The value a patch field carries, with UNSET flattened to None.

    Every use of this is paired with a separate `is not UNSET` flag in the
    same statement, so the None this returns for an absent field is never
    read: the CASE it feeds takes its ELSE branch. Flattening rather than
    raising keeps the two parameters of a field independent -- the flag says
    whether to write, the value says what -- which is what lets one uniform
    pair of parameters serve a field whose real value may itself be None.
    """
    if isinstance(field, Unset):
        return None

    return field


# Every column an IssueEntity is built from, as one expression list shared by
# the statements below.
#
# This is interpolated into the SQL with an f-string, which in this repository
# needs a defence. "Parameterized SQL only" is a rule about *values*: a value
# reaching a statement by concatenation is an injection, which is why every
# one of them below arrives as $n. This constant is not a value. It is a
# module-level literal that no input can influence, chosen instead of five
# copies of the same seventeen lines because the copies are how a column gets
# added to the entity, to four statements, and forgotten in the fifth -- a
# mistake that surfaces as a KeyError from `_to_entity` at runtime, on
# whichever path happened not to be exercised.
#
# `team_key` is a correlated subquery rather than a join, deliberately. A join
# would restructure `list`, whose FROM and WHERE are the keyset walk that
# migrations 002 and 006 index and that this project's pagination suite reads
# statement-by-statement; a scalar subquery leaves that shape untouched and
# costs a primary-key lookup against a table with one row per team.
ISSUE_COLUMNS = """
                issues.id,
                issues.team_id,
                (
                    SELECT teams.key
                    FROM teams
                    WHERE teams.workspace_id = issues.workspace_id
                        AND teams.id = issues.team_id
                ) AS team_key,
                issues.number,
                issues.title,
                issues.description,
                issues.priority,
                issues.workflow_state_id,
                issues.assignee_id,
                issues.creator_id,
                issues.estimate,
                issues.due_date,
                issues.cycle_id,
                issues.project_id,
                issues.milestone_id,
                issues.completed_at,
                issues.archived_at,
                issues.created_at,
                issues.updated_at
"""


# The sort key each ordering compares by, as SQL.
#
# `NULLIF(issues.priority, 0)` and not `issues.priority`: 0 is "no priority",
# not "the lowest one" -- 1 is Urgent and 4 is Low -- so the raw column sorts
# untriaged issues either above Urgent or below Low, and a person who asked
# for "by priority" wanted neither. Nulling it out puts 1..4 in urgency order
# ascending and leaves the untriaged at the end, which is the same place the
# undated issues sit under DUE_DATE. `app.domain.issues.order_key` computes
# the matching value for the cursor and must keep agreeing with this.
_ORDER_KEYS: dict[IssueOrderField, str] = {
    IssueOrderField.PRIORITY: "NULLIF(issues.priority, 0)",
    IssueOrderField.CREATED_AT: "issues.created_at",
    IssueOrderField.UPDATED_AT: "issues.updated_at",
    IssueOrderField.DUE_DATE: "issues.due_date",
}

# The keys that can be NULL for a row, which is what decides whether the
# keyset comparison needs its null branches at all. `created_at` and
# `updated_at` are NOT NULL columns, so a cursor over them never carries a
# null key and the simple row-value comparison is total by itself.
_NULLABLE_ORDER_KEYS = frozenset(
    {IssueOrderField.PRIORITY, IssueOrderField.DUE_DATE},
)


# Each relative due window, as the predicate it becomes.
#
# `CURRENT_DATE` and not a bound parameter, which is the whole point of the
# window existing rather than a client sending two dates. The server decides
# what today is, once, for everybody looking at the workspace -- see
# `DueWindow` and migrations/029_estimates_dates.sql on why that clock is UTC.
#
# These carry no `$n` and are interpolated as module-level literals, which is
# the same rule `ISSUE_COLUMNS` above is interpolated under: "parameterized SQL
# only" is a rule about VALUES, and there is no value here. Nothing a client
# sends reaches this text -- the enum member selects a whole clause, so an
# unknown one is a KeyError in this process rather than a fragment on the wire.
#
# EVERY ONE IS A BOUND ON `due_date`, deliberately, so that all four can be
# answered from `issues_workspace_live_due_date_id_idx` (migration 015) as
# ranges under the tenant equality rather than as predicates applied to rows
# that had to be fetched first:
#
#   * OVERDUE   -- `< CURRENT_DATE`, and NULL is excluded for free, because
#                  `NULL < x` is NULL rather than true. An issue with no due
#                  date has missed nothing.
#   * TODAY     -- an equality.
#   * THIS_WEEK -- a half-open range: today, and the six days after it. The
#                  upper bound is exclusive so that the arithmetic reads as
#                  "seven days" once rather than as "six" with a comment
#                  explaining the seventh.
#   * NO_DUE_DATE -- `IS NULL`, which a btree indexes, so the undated issues
#                  are a range at the far end of that index. The same property
#                  015 relies on for `assigneeId: null`.
_DUE_WINDOW_CLAUSES: dict[DueWindow, str] = {
    DueWindow.OVERDUE: "issues.due_date < CURRENT_DATE",
    DueWindow.TODAY: "issues.due_date = CURRENT_DATE",
    DueWindow.THIS_WEEK: (
        "issues.due_date >= CURRENT_DATE"
        f" AND issues.due_date < CURRENT_DATE + {THIS_WEEK_DAYS}"
    ),
    DueWindow.NO_DUE_DATE: "issues.due_date IS NULL",
}


class _Predicates:
    """Optional WHERE clauses and the positional parameters they read.

    Hand-built, and CLAUDE.md's SQLAlchemy Core exception is declined
    deliberately. What a compiler would buy here is this class: a list of
    predicate strings and a list of values whose index is the parameter
    number. What it would cost is a dependency with an expression language of
    its own, which every reader of `list` below would then have to know in
    order to answer the only question that matters about this file -- whether
    the tenant predicate is ANDed onto every statement. Nothing here needs
    dialect portability, composable subqueries, or joins it cannot spell.

    Values NEVER reach the SQL. `bind` appends a value and hands back the
    `$n` that reads it, so a filter value can only ever arrive as a
    parameter; the clause templates are module-local literals no input
    reaches.

    Constructed with the workspace as the first value, so `$1` is the
    authorized workspace in every clause below -- including the subqueries,
    which scope themselves to it rather than to a tenant read off a row.
    """

    def __init__(self, workspace_id: UUID) -> None:
        self._values: list[object] = [workspace_id]
        self._clauses: list[str] = []

    def bind(self, value: object) -> str:
        self._values.append(value)

        return f"${len(self._values)}"

    def add(self, clause: str) -> None:
        self._clauses.append(clause)

    @property
    def sql(self) -> str:
        return "".join(f"\n                    AND {c}" for c in self._clauses)

    @property
    def values(self) -> list[object]:
        return self._values


def _add_filters(predicates: _Predicates, issue_filter: IssueFilter) -> None:
    """Turn a filter into predicates, one per field the caller named.

    Every one of these NARROWS. They are ANDed onto `issues.workspace_id =
    $1`, never substituted for it, so an id from another workspace -- a
    project, a cycle, a team, a label, an assignee -- intersects with nothing
    instead of selecting that workspace's rows. The two that reach other
    tables carry `$1` themselves for the same reason.

    Only the clauses the caller actually asked for are emitted, which is a
    reversal of what `list` used to do with `team_id` (one statement, a bound
    NULL, and `($n IS NULL OR ...)`). With eight optional filters that trick
    stops being one statement and starts being a WHERE clause the planner
    cannot see through: every filter would be opaque until execution, so a
    highly selective one would be costed as if it matched everything. The
    price is that the statement text varies with the filter COMBINATION, so
    asyncpg caches one prepared statement per combination in use. That is
    bounded by the screens the product has, and it is the right way round:
    the plan follows the query.
    """
    if issue_filter.team_id is not UNSET:
        predicates.add(f"issues.team_id = {predicates.bind(issue_filter.team_id)}")

    if issue_filter.assignee_id is not UNSET:
        predicates.add(
            _maybe_null("issues.assignee_id", issue_filter.assignee_id, predicates)
        )

    if issue_filter.workflow_state_id is not UNSET:
        predicates.add(
            f"issues.workflow_state_id = {predicates.bind(issue_filter.workflow_state_id)}"
        )

    if issue_filter.state_category is not UNSET:
        # A category is a property of the workflow state, not of the issue, so
        # this is the one filter that has to leave the table. The subquery is
        # scoped to $1: a state from another tenant resolves to no ids rather
        # than to rows this workspace cannot see. `workflow_states` holds a
        # handful of rows per team, so the planner hashes it and probes.
        predicates.add(
            f"""issues.workflow_state_id IN (
                        SELECT workflow_states.id
                        FROM workflow_states
                        WHERE workflow_states.workspace_id = $1
                            AND workflow_states.type = """
            f"""{predicates.bind(issue_filter.state_category.value)}
                    )"""
        )

    if issue_filter.label_id is not UNSET:
        # EXISTS rather than a join, so an issue wearing the label twice --
        # which `issue_labels_pkey` forbids, but which a join would have to be
        # trusted about -- cannot duplicate a row and corrupt the page count.
        predicates.add(
            f"""EXISTS (
                        SELECT 1
                        FROM issue_labels
                        WHERE issue_labels.workspace_id = $1
                            AND issue_labels.issue_id = issues.id
                            AND issue_labels.label_id = """
            f"""{predicates.bind(issue_filter.label_id)}
                    )"""
        )

    if issue_filter.priority is not UNSET:
        predicates.add(f"issues.priority = {predicates.bind(issue_filter.priority)}")

    if issue_filter.project_id is not UNSET:
        predicates.add(
            _maybe_null("issues.project_id", issue_filter.project_id, predicates)
        )

    if issue_filter.cycle_id is not UNSET:
        predicates.add(
            _maybe_null("issues.cycle_id", issue_filter.cycle_id, predicates)
        )

    if issue_filter.due_window is not UNSET:
        predicates.add(_DUE_WINDOW_CLAUSES[issue_filter.due_window])

    # The absolute range, ANDed alongside whatever window was asked for. Both
    # ends inclusive, which is what a person dragging a calendar means by
    # "these two days"; an exclusive end would make the last day of a sprint
    # not part of it.
    if issue_filter.due_after is not UNSET:
        predicates.add(f"issues.due_date >= {predicates.bind(issue_filter.due_after)}")

    if issue_filter.due_before is not UNSET:
        predicates.add(f"issues.due_date <= {predicates.bind(issue_filter.due_before)}")


def _maybe_null(column: str, value: UUID | None, predicates: _Predicates) -> str:
    """`column = $n`, or `column IS NULL` where the caller asked for nothing.

    `= NULL` is never true in SQL, so a filter that bound None would return
    an empty page instead of the unassigned issues -- silently, which is why
    this is a branch and not a bound parameter.
    """
    if value is None:
        return f"{column} IS NULL"

    return f"{column} = {predicates.bind(value)}"


def _add_keyset(
    predicates: _Predicates,
    *,
    order: IssueOrder,
    cursor: IssueListCursor,
) -> None:
    """Resume the walk after one row, in the ordering that row was read under.

    The ordering is `(key, id)` and `id` is unique, so it is total: no two
    rows compare equal, which is the property that makes a keyset walk neither
    skip nor repeat. Everything below is about the one thing a row-value
    comparison cannot express, which is where the NULL keys sit.

    PostgreSQL's defaults are used rather than an explicit NULLS clause, and
    that is a deliberate index decision: `ASC` is NULLS LAST and `DESC` is
    NULLS FIRST, so one index in ascending order serves both directions --
    forwards for one, backwards for the other. Spelling `DESC NULLS LAST`
    instead would read slightly better on screen and would need a second
    index to serve at all.

    So, with the direction fixing where the nulls are:

    * ASC, key present -- every null row is still ahead of us, plus the
      non-null rows past the cursor.
    * ASC, key null -- we are already among the nulls; only the nulls with a
      greater id remain.
    * DESC, key present -- the nulls are behind us, and the row comparison
      already excludes them: `(NULL, id) < (k, i)` is NULL, not true.
    * DESC, key null -- the rest of the nulls, and then everything non-null.
    """
    key_sql = _ORDER_KEYS[order.field]
    descending = order.direction is OrderDirection.DESC
    operator = "<" if descending else ">"

    if cursor.key is None:
        id_param = predicates.bind(cursor.id)

        if descending:
            predicates.add(
                f"({key_sql} IS NOT NULL"
                f" OR ({key_sql} IS NULL AND issues.id {operator} {id_param}))"
            )
        else:
            predicates.add(f"({key_sql} IS NULL AND issues.id {operator} {id_param})")

        return

    key_param = predicates.bind(cursor.key)
    id_param = predicates.bind(cursor.id)
    row_value = f"({key_sql}, issues.id) {operator} ({key_param}, {id_param})"

    if not descending and order.field in _NULLABLE_ORDER_KEYS:
        predicates.add(f"({key_sql} IS NULL OR {row_value})")

        return

    predicates.add(row_value)


class IssueRepository:
    """SQL access for `issues`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement here is scoped to one workspace, and the scope arrives
    as a required keyword argument -- never a default, never an attribute
    set once on the instance. Two things follow, and both are the reason for
    the shape. A caller cannot reach this class without having decided which
    tenant it is addressing, because no signature here omits the question.
    And `scope=` appears literally at every call site, so "does this query
    cross tenants" is answered by reading the call rather than by tracing
    back to whatever constructed the repository.

    The writes carry a second discipline, inherited from `create` and now
    applied to `update` as well: no statement here checks a tenant rule by
    reading first. An assignee from another workspace, a workflow state from
    another team, a team from another workspace -- each is refused by a
    composite foreign key as part of the same statement that would have
    written it. A SELECT beforehand would be a second copy of each of those
    rules, separated from the write by a round trip in which the fact it
    checked can change.

    Every read and every write carries `archived_at IS NULL`. Archiving
    removes an issue from the product, so a statement here that still
    resolved one would leave every caller to remember a filter this class
    can apply once.
    """

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """The issue with this id in this workspace, or nothing.

        The workspace is part of the lookup, not a check applied to the row
        afterwards. That distinction is the security property: an issue
        belonging to another tenant produces exactly the same answer as an
        id that exists nowhere at all, so a caller holding a guessed or
        leaked id learns nothing by asking. Fetching by id first and then
        comparing `row["workspace_id"]` would return the same None while
        having already read another tenant's row into this process.

        `archived_at IS NULL` joins that predicate rather than sitting
        beside it, so an archived issue is also the same answer.
        """
        row = await connection.fetchrow(
            f"""
            SELECT
{ISSUE_COLUMNS}
            FROM issues
            WHERE workspace_id = $1 AND id = $2 AND archived_at IS NULL
            """,
            scope.workspace_id,
            issue_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def find_many_by_ids(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_ids: Sequence[UUID],
    ) -> list[IssueEntity]:
        """Every live issue in this workspace among these ids, in no set order.

        One statement for many ids, because the alternative is one statement
        per row on any list that resolves an issue per item --
        `Notification.issue` is the first, and an inbox page of twenty-five
        would otherwise be twenty-five round trips.

        Ids belonging to another workspace simply do not come back: the tenant
        predicate leads here as everywhere, so a batch cannot be used to learn
        anything a single `get_by_id` would have withheld. That matters more
        here than usual, because the ids arrive from ROWS rather than from a
        client -- a notification names its issue -- and a mismatched row must
        still not be able to surface another tenant's issue.

        `archived_at IS NULL`, matching every other read on this class. A
        notification about an issue that has since been archived resolves to
        nothing, which is the same answer the issue itself gives everywhere
        else in the API.

        The result is a list rather than a dict keyed by the ids asked for:
        building that mapping means deciding what an absent id means, which is
        the caller's decision and not the repository's.
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

        return [self._to_entity(row) for row in rows]

    async def find_estimate_scale(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> EstimateScale | None:
        """The unit this issue's team estimates in, or nothing.

        A statement about `teams` in the issues repository, which needs a word.
        The question is "what does THIS ISSUE's estimate count", so the issue is
        what it is asked about and the team is a join on the way -- the same
        judgement `ISSUE_COLUMNS` already makes for `team_key`, which is a
        correlated subquery against the same table for the same reason. A
        method on `TeamRepository` would have to be handed a team id, which is
        the value the caller does not have: an update carries an issue.

        Used by `IssueService` to check an estimate against the scale before it
        is written. That check cannot be a database constraint -- the two
        columns are in different tables and a CHECK cannot join -- so this is
        the read the rule is enforced from; see
        migrations/029_estimates_dates.sql on why a trigger was not the answer
        either.

        The join is on the ISSUE's own stored `(workspace_id, team_id)`, so the
        scale returned is the one belonging to the team the database has, not
        one derived from anything a caller sent. `issues_team_fk` guarantees
        the join finds a row for every live issue.

        None means the issue is not here to ask about -- nonexistent, another
        tenant's, or archived -- which is the answer every read on this class
        gives for those three, and the caller treats it the same way: there is
        nothing to validate, and the write that follows will answer None too.
        """
        scale = await connection.fetchval(
            """
            SELECT teams.estimate_scale
            FROM issues
            JOIN teams
                ON teams.workspace_id = issues.workspace_id
                AND teams.id = issues.team_id
            WHERE issues.workspace_id = $1
                AND issues.id = $2
                AND issues.archived_at IS NULL
            """,
            scope.workspace_id,
            issue_id,
        )

        if scale is None:
            return None

        # Constructed through the enum rather than passed through as a string,
        # so a scale the domain does not know about fails here -- at the one
        # place that reads it out of a row -- rather than flowing on as a str
        # that every `is`-comparison in the application quietly answers False
        # for. The same move `TeamRepository._to_workflow_state` makes.
        return EstimateScale(scale)

    async def lock_snapshot(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueSnapshot | None:
        """The fields activity reports on, as they are now, held for the write.

        `FOR UPDATE` is the whole point and not a precaution. Without it this
        is an ordinary read: another transaction may commit between it and the
        UPDATE that follows, and the history would then record "from A to C"
        for a row that went A to B to C -- a wrong claim about the past, which
        is worse than a missing one. The lock is taken by the same transaction
        that is about to write, so the value read is the value updated.

        It is held until that transaction ends, which serialises concurrent
        edits of ONE issue. That is the cost, and it is the right one: two
        simultaneous edits of the same issue already race for the last word,
        and this makes the loser's history honest rather than making the
        contention new.

        Scoped by workspace, so another tenant's issue answers None -- the
        same answer an id that exists nowhere gives.

        `archived_at` is NOT in the predicate, unlike the reads. The writes
        this precedes disagree about archived issues -- `update` excludes
        them, `set_cycle` and `set_project` do not -- so filtering here would
        change what one of them does. The write's own result decides: no row
        updated means no activity to record.
        """
        row = await connection.fetchrow(
            """
            SELECT
                title,
                priority,
                workflow_state_id,
                assignee_id,
                project_id,
                cycle_id
            FROM issues
            WHERE workspace_id = $1 AND id = $2
            FOR UPDATE
            """,
            scope.workspace_id,
            issue_id,
        )

        if row is None:
            return None

        return IssueSnapshot(
            title=row["title"],
            priority=row["priority"],
            workflow_state_id=row["workflow_state_id"],
            assignee_id=row["assignee_id"],
            project_id=row["project_id"],
            cycle_id=row["cycle_id"],
        )

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        number: int,
        workflow_state_id: UUID,
        title: str,
        description: str | None,
        priority: int,
        assignee_id: UUID | None,
        creator_id: UUID | None,
        estimate: int | None,
        due_date: date | None,
    ) -> IssueEntity:
        """Insert one issue into this workspace, against this team.

        `number` and `workflow_state_id` are supplied by the caller rather
        than defaulted here, and both are required for the same reason the
        tenancy columns are: 005 made them NOT NULL with no default, so a
        caller that forgets one is refused by the database instead of
        writing a row with an invented identifier or an unset status. The
        number in particular has to come from the caller, because it must be
        allocated inside the same transaction as this insert -- see
        `TeamService.allocate_issue_number`.

        Both tenancy columns are written explicitly. Neither carries a
        database default, deliberately -- migrations/002_tenancy.sql:102-109
        argues that a default would turn every insert that forgot a
        workspace into a silent write against the bootstrap tenant instead
        of a loud rejection -- so omitting either here is a NOT NULL
        violation rather than a quiet mis-filing.

        `completed_at` is not written, and must not be. The state a new
        issue starts in is non-terminal by construction -- the service
        resolves it through `TeamService.default_workflow_state_id`, which
        selects the `unstarted` category -- so the column's NULL default is
        already the answer the completed_at rule gives. Writing it here
        would put that rule in a second place; see `IssueService`.

        Neither the team, the assignee nor the workflow state is checked
        against the workspace before the insert. `issues_team_fk`,
        `issues_assignee_fk` and `issues_workflow_state_fk` are composite
        foreign keys onto `teams (workspace_id, id)`,
        `workspace_members (workspace_id, user_id)` and
        `workflow_states (workspace_id, team_id, id)` respectively, so the
        server refuses each mismatch as part of this statement. A SELECT
        here first would be a second, weaker copy of those rules: weaker
        because it is a separate statement, so a membership could be revoked
        between the two, and weaker because it would then be two places that
        have to agree. A mismatched pair therefore surfaces as
        ForeignKeyViolationError, which the service translates for the two
        constraints a client can actually provoke and lets propagate for the
        rest.

        `cycle_id` is not written and carries no default, so a new issue is
        in no cycle. Accepting one here would mean validating a cycle
        against a team the caller has only just named; `set_cycle` validates
        it against the team the database has already stored.
        """
        row = await connection.fetchrow(
            f"""
            INSERT INTO issues (
                workspace_id,
                team_id,
                number,
                workflow_state_id,
                title,
                description,
                priority,
                assignee_id,
                creator_id,
                estimate,
                due_date
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            team_id,
            number,
            workflow_state_id,
            title,
            description,
            priority,
            assignee_id,
            creator_id,
            estimate,
            due_date,
        )

        return self._to_entity(row)

    async def update(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        patch: IssuePatch,
    ) -> IssueEntity | None:
        """Apply a patch to one live issue in this workspace, or nothing.

        One statement, and that is the point of its shape. The alternative
        -- SELECT the row, merge in Python, UPDATE it back -- reads a value
        and writes a value derived from it across two round trips, so two
        concurrent edits each write a row computed from a state that is
        already stale by the time it lands, and the later write silently
        discards the earlier one. Here every field is either replaced by a
        bound parameter or left as the column's own value, evaluated by the
        server while it holds the row.

        Each field is `CASE WHEN <flag> THEN <value> ELSE <column> END` --
        two parameters per field rather than one. The flag is what
        distinguishes "set this to nothing" from "do not touch this", which
        a single nullable parameter cannot: without it, clearing an assignee
        and not mentioning the assignee are the same request. The uniform
        two-parameter shape is used even for the columns that are NOT NULL,
        where a bare NULL could have meant "unchanged" unambiguously, so
        that reading this statement needs no per-column reasoning about
        nullability and so that a column later becoming nullable does not
        silently change what an update means.

        Scoped exactly as the reads are. Updating an issue in another
        workspace matches no row and returns None, which is the same answer
        as an id that exists nowhere -- so, as with `get_by_id`, the
        operation cannot be used to discover that someone else's issue
        exists. `archived_at IS NULL` is in the predicate for the same
        reason it is in the reads: an archived issue is not there to edit.

        `completed_at` is assigned on every update rather than only when the
        state changes, and it is computed from the state the row will end up
        in -- see `IssueService` for the rule itself. Recomputing
        unconditionally is what makes the rule self-repairing: a row whose
        `completed_at` disagrees with its state, however it got that way, is
        corrected by the next write instead of carrying the disagreement
        forever.

        The workflow-state subquery is scoped to the workspace. Without that
        predicate a state id from another tenant would be read here to
        decide `completed_at`; the write would still be refused by
        `issues_workflow_state_fk`, so nothing would be corrupted, but the
        row would have been read, and a statement that reads another
        tenant's row is not one to leave in place because its result happens
        to be discarded.
        """
        row = await connection.fetchrow(
            f"""
            WITH target AS (
                SELECT
                    issues.id,
                    CASE
                        WHEN $9 THEN $10::UUID
                        ELSE issues.workflow_state_id
                    END AS workflow_state_id
                FROM issues
                WHERE issues.workspace_id = $1
                    AND issues.id = $2
                    AND issues.archived_at IS NULL
            )
            UPDATE issues
            SET
                title = CASE WHEN $3 THEN $4::TEXT ELSE issues.title END,
                description = CASE
                    WHEN $5 THEN $6::TEXT
                    ELSE issues.description
                END,
                priority = CASE
                    WHEN $7 THEN $8::SMALLINT
                    ELSE issues.priority
                END,
                workflow_state_id = target.workflow_state_id,
                assignee_id = CASE
                    WHEN $11 THEN $12::UUID
                    ELSE issues.assignee_id
                END,
                estimate = CASE
                    WHEN $13 THEN $14::INTEGER
                    ELSE issues.estimate
                END,
                due_date = CASE
                    WHEN $15 THEN $16::DATE
                    ELSE issues.due_date
                END,
                completed_at = CASE
                    WHEN (
                        SELECT workflow_states.type
                        FROM workflow_states
                        WHERE workflow_states.id = target.workflow_state_id
                            AND workflow_states.workspace_id = $1
                    ) = ANY($17::TEXT[])
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
            issue_id,
            patch.title is not UNSET,
            _value(patch.title),
            patch.description is not UNSET,
            _value(patch.description),
            patch.priority is not UNSET,
            _value(patch.priority),
            patch.workflow_state_id is not UNSET,
            _value(patch.workflow_state_id),
            patch.assignee_id is not UNSET,
            _value(patch.assignee_id),
            patch.estimate is not UNSET,
            _value(patch.estimate),
            patch.due_date is not UNSET,
            _value(patch.due_date),
            list(TERMINAL_STATE_CATEGORIES),
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def archive(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """Take one issue off the board, or answer nothing.

        `archived_at IS NULL` in the predicate makes this match nothing
        rather than succeed on an already-archived issue, which keeps the
        timestamp meaning "when this was archived" instead of "when it was
        last archived again". The caller cannot tell that case from a
        nonexistent id or another tenant's -- all three are None -- and that
        is correct rather than merely convenient: an archived issue is
        invisible to every read on this class, so a caller who cannot see it
        must not be able to learn it exists by trying to archive it.

        The row is returned rather than a row count. An archive is a state
        change the client has to render, and returning the issue lets it do
        that from the mutation's own result instead of refetching a row
        that, by then, no read here will hand back.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues
            SET archived_at = now(),
                updated_at = now()
            WHERE workspace_id = $1
                AND id = $2
                AND archived_at IS NULL
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def search(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        query: str,
        limit: int,
    ) -> list[IssueEntity]:
        """The workspace's live issues matching a free-text query, best first.

        `websearch_to_tsquery` and not `to_tsquery`, which is the security half
        of this statement. `to_tsquery` takes tsquery *syntax* -- `&`, `|`,
        `!`, `:*` -- so a user's search box would be a small expression
        language, and an unbalanced quote or a stray `&` raises a
        SyntaxError from the server rather than finding nothing. The web-search
        parser instead takes what a person types: bare words are ANDed, `"a b"`
        is a phrase, `or` and `-` do what they do everywhere else, and no input
        is a syntax error. Punctuation alone parses to an empty tsquery, which
        `@@` answers false for every row -- so garbage returns nothing rather
        than erroring or, worse, matching everything.

        The configuration is named -- `'english'` -- and must stay the same
        name migrations/011_search.sql generates the column under. A query
        parsed under one dictionary and a vector built under another agree only
        by coincidence, and the disagreement is silent: no error, just results
        that quietly stop containing the row you were looking for.

        The tsquery is spelled out twice rather than joined in from a
        one-row subquery. `websearch_to_tsquery('english', $2)` over a bound
        parameter folds to a constant the planner can push into the GIN index;
        the same expression reached through `FROM ..., websearch_to_tsquery(...)
        AS q` is a join qualification, which is how the index quietly stops
        being used.

        Ordered by rank and then by `id DESC`, and the second half is not
        decoration: `ts_rank` produces ties constantly -- two issues whose
        titles both contain the term once score identically -- so without a
        unique tie-break the same query returns the same rows in a different
        order on each request, which reads to a user as results that shuffle
        while they look at them.

        `ts_rank`, not `ts_rank_cd`. Both honour the A/B weighting that puts a
        title match above a description match, which is the ranking this
        schema actually declares; cover density additionally rewards query
        terms appearing close together, which is a property of prose and not
        of issue titles.

        Tenant-scoped in the WHERE clause, exactly as every other read here is,
        and `archived_at IS NULL` beside it. Both predicates are in the partial
        composite index migration 011 builds, so neither is a filter applied to
        rows that had to be fetched first.
        """
        rows = await connection.fetch(
            f"""
            SELECT
{ISSUE_COLUMNS}
            FROM issues
            WHERE workspace_id = $1
                AND archived_at IS NULL
                AND search_vector @@ websearch_to_tsquery('english', $2)
            ORDER BY
                ts_rank(
                    search_vector,
                    websearch_to_tsquery('english', $2)
                ) DESC,
                id DESC
            LIMIT $3
            """,
            scope.workspace_id,
            query,
            limit,
        )

        return [self._to_entity(row) for row in rows]

    async def get_by_identifier(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        team_key: str,
        number: int,
    ) -> IssueEntity | None:
        """The issue a human calls `ENG-42`, or nothing.

        A different lookup from `search` and not a special case of it. An
        identifier is a key, so this is an equality on two unique indexes --
        teams_workspace_key_unique for the key, issues_team_number_key for the
        number, both from migrations/005_team_workflows.sql -- rather than a
        ranked scan that would have to hope the digits survived stemming.

        The team is resolved by a scalar subquery bound to the SAME `$1` the
        outer predicate uses. A team from another workspace therefore resolves
        to NULL and matches no issue, so this cannot be used to read across a
        tenant boundary even with a key guessed correctly.

        `archived_at IS NULL` for the reason every read here carries it: an
        archived issue is not in the product, and answering with one because
        the caller happened to know its identifier would be the one way back
        in.
        """
        row = await connection.fetchrow(
            f"""
            SELECT
{ISSUE_COLUMNS}
            FROM issues
            WHERE issues.workspace_id = $1
                AND issues.archived_at IS NULL
                AND issues.number = $3
                AND issues.team_id = (
                    SELECT teams.id
                    FROM teams
                    WHERE teams.workspace_id = $1 AND teams.key = $2
                )
            """,
            scope.workspace_id,
            team_key,
            number,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_filter: IssueFilter,
        order: IssueOrder,
        limit: int,
        after: IssueListCursor | None,
    ) -> list[IssueEntity]:
        """Keyset page of one workspace's live issues, in one ordering.

        `limit` is expected to already be `first + 1` so the caller can
        detect a following page. No OFFSET at any page size: the cursor is a
        row-value comparison, so the cost of page N is the cost of page 1.

        The tenant predicate leads and is ANDed with everything else rather
        than folded into any of it. Widening the row-value comparison to
        `(workspace_id, created_at, id) < (...)` would put workspaces into
        the ordering, which is how a page walk falls out of one tenant and
        into whichever one sorts next; the workspace is an equality, the
        filters are equalities, and only `(key, id)` is the keyset.

        `archived_at IS NULL` is ANDed on for the same reason and with the
        same care -- it is another equality-shaped filter, not part of the
        ordering key. Migration 006 adds a partial index carrying that
        predicate, so archived rows are absent from the index this walk reads
        rather than fetched and discarded; without it the cost of a page
        would grow with every issue the workspace had ever created instead of
        with the ones still on its board. Migration 015 keeps every ordering
        index partial on it for the same reason.

        Every filter is a NARROWING and none is ever a widening -- see
        `_add_filters`, which is where a client-supplied id from another
        workspace becomes an empty page rather than a leak.
        """
        predicates = _Predicates(scope.workspace_id)
        _add_filters(predicates, issue_filter)

        if after is not None:
            _add_keyset(predicates, order=order, cursor=after)

        direction = "DESC" if order.direction is OrderDirection.DESC else "ASC"
        key_sql = _ORDER_KEYS[order.field]
        limit_param = predicates.bind(limit)

        rows = await connection.fetch(
            f"""
                SELECT
{ISSUE_COLUMNS}
                FROM issues
                WHERE issues.workspace_id = $1
                    AND issues.archived_at IS NULL{predicates.sql}
                ORDER BY {key_sql} {direction}, issues.id {direction}
                LIMIT {limit_param}
            """,
            *predicates.values,
        )

        return [self._to_entity(row) for row in rows]

    async def count(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_filter: IssueFilter,
    ) -> int:
        """How many live issues in this workspace match, ignoring paging.

        The same WHERE clause as `list` minus the keyset, built by the same
        code so the two cannot disagree about what a filter means. No ORDER
        BY and no LIMIT: an aggregate over a set has no position in it.

        This is a SECOND aggregate over the same predicate, and it is worth
        paying exactly where a number is the answer rather than a decoration
        -- a board column that has to say "8 in progress" when it has paged
        in three of them, a filter chip that reports what it would select. It
        is not worth paying for an infinite scroll, which needs to know only
        whether there is more, and `pageInfo.hasNextPage` answers that from
        the page it already fetched. `IssueConnection.totalCount` is a
        resolver for that reason: a document that does not select it does not
        run this.

        `count(*)` and not `count(id)`: identical here, since `id` is NOT
        NULL, and the star form is the one the planner special-cases.
        """
        predicates = _Predicates(scope.workspace_id)
        _add_filters(predicates, issue_filter)

        total = await connection.fetchval(
            f"""
                SELECT count(*)
                FROM issues
                WHERE issues.workspace_id = $1
                    AND issues.archived_at IS NULL{predicates.sql}
            """,
            *predicates.values,
        )

        return int(total)

    async def set_cycle(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        cycle_id: UUID | None,
    ) -> IssueEntity | None:
        """Move one issue into a cycle, or out of whatever cycle it is in.

        None for `cycle_id` is the unassignment, and it is a real value here
        rather than a skipped column: `SET cycle_id = $3` with a NULL bound
        to it is one statement whether the issue is joining a cycle or
        leaving one.

        Nothing checks that the cycle belongs to this issue's team, and
        nothing may. `issues_cycle_fk` references
        `cycles (workspace_id, team_id, id)` using the issue's OWN stored
        workspace and team, so a cycle from another team -- or another
        tenant -- is refused by the server as part of this statement, and it
        is refused against the issue's committed team rather than against
        whatever a preceding SELECT read. A pre-check would be a second,
        weaker copy of that rule: weaker because the issue could be moved to
        another team between the two statements, and weaker because two
        places that have to agree eventually will not. The violation reaches
        IssueService, which is the layer that decides what a client is told.

        `updated_at` is stamped here because the issue changed; the column
        has no trigger behind it.

        None means no row matched the id in this workspace -- the same
        answer for an issue that does not exist and one belonging to another
        tenant.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues
            SET cycle_id = $3,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
            cycle_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def set_project(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        project_id: UUID | None,
        milestone_id: UUID | None,
    ) -> IssueEntity | None:
        """Place one issue in a project and milestone, or take it out of both.

        Both columns are written together on purpose. They are not two
        independent settings: `issues_milestone_requires_project` refuses a
        milestone without a project, and `issues_milestone_fk` refuses a
        milestone belonging to a different project than the one on the same
        row. A method that set them one at a time would have to pass through a
        state the schema forbids in order to reach a state it allows, so the
        write is a single statement that moves the issue from one legal pair
        to another.

        Nothing is checked before the UPDATE. The three ways this can be
        wrong -- a project from another workspace, a milestone from another
        project, a milestone with no project -- are the three constraints
        migrations/009_projects.sql declares, and each surfaces as a
        PostgresError naming the constraint it broke. IssueService translates
        exactly those names and nothing else.

        Returning None means no row in this workspace has that id, which is
        the same answer another tenant's issue produces.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues
            SET
                project_id = $3,
                milestone_id = $4,
                updated_at = now()
            WHERE workspace_id = $1 AND id = $2
            RETURNING
{ISSUE_COLUMNS}
            """,
            scope.workspace_id,
            issue_id,
            project_id,
            milestone_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def clear_project(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> None:
        """Detach every issue from one project, milestones included.

        Called on the way to deleting the project. `milestone_id` is cleared
        in the same statement because every milestone of that project is about
        to go too, and an issue left pointing at one would make
        `issues_milestone_requires_project` false the instant `project_id`
        became NULL -- so clearing only the project is a state the server
        would refuse, not merely one that would look odd.
        """
        await connection.execute(
            """
            UPDATE issues
            SET project_id = NULL, milestone_id = NULL, updated_at = now()
            WHERE workspace_id = $1 AND project_id = $2
            """,
            scope.workspace_id,
            project_id,
        )

    async def clear_milestone(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        milestone_id: UUID,
    ) -> None:
        """Detach every issue from one milestone, leaving them in the project.

        The mirror image of `clear_project`, and deliberately not symmetrical
        with it: deleting a milestone is a change to the plan inside a
        project, not a removal of the work from it.
        """
        await connection.execute(
            """
            UPDATE issues
            SET milestone_id = NULL, updated_at = now()
            WHERE workspace_id = $1 AND milestone_id = $2
            """,
            scope.workspace_id,
            milestone_id,
        )

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> IssueEntity:
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


# The same mapping under a name a second module can import.
#
# app/repositories/embeddings.py selects ISSUE_COLUMNS and has to build the
# same entity from it -- its hybrid and duplicate statements are anchored on
# `issue_embeddings` but answer with issues -- and restating these nineteen
# assignments there is exactly how the two copies come to disagree the next
# time a column joins the entity. One expression list, one mapping, two
# callers.
issue_from_row = IssueRepository._to_entity
