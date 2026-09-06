from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.issues import IssueEntity
from app.domain.relations import (
    SYMMETRIC_TYPES,
    DuplicateRelationError,
    IssueRelationEntity,
    RelatedIssueNotFoundError,
    RelationEndpoint,
    RelationType,
    invert,
)
from app.domain.tenancy import WorkspaceScope


# The advisory-lock class for sub-issue re-parenting. See
# `RelationRepository.lock_parenting` for what it serialises and why.
#
# Two arguments, not one. PostgreSQL's one-argument pg_advisory_xact_lock and
# its two-argument form occupy DIFFERENT lock spaces -- the tag carries an
# extra field recording which form was used -- so this cannot collide with
# `scripts.apply_migration.ADVISORY_LOCK_KEY`, which takes the one-argument
# form. That is a guarantee of the lock manager rather than a coincidence of
# the two constants, so it survives someone changing either number.
PARENTING_LOCK_CLASS = 0x56504152

# The issue columns every read here returns, aliased so one statement can
# select an issue that is not the row it is keyed on.
#
# `workspace_id` and `parent_id` are deliberately absent, matching
# `IssueRepository`: `IssueEntity` carries no tenant and no edges, so
# returning them would either be dropped on the floor or tempt someone to
# widen the entity with fields whose whole purpose is to be unforgeable
# server-side. Everything else the entity carries must be here -- it is
# built from these rows, and a column missing is a KeyError at runtime on
# whichever relation path happens not to be exercised.
_ISSUE_COLUMNS = """
    {alias}.id,
    {alias}.team_id,
    (
        SELECT teams.key
        FROM teams
        WHERE teams.workspace_id = {alias}.workspace_id
            AND teams.id = {alias}.team_id
    ) AS team_key,
    {alias}.number,
    {alias}.title,
    {alias}.description,
    {alias}.priority,
    {alias}.workflow_state_id,
    {alias}.assignee_id,
    {alias}.creator_id,
    {alias}.estimate,
    {alias}.due_date,
    {alias}.cycle_id,
    {alias}.project_id,
    {alias}.milestone_id,
    {alias}.completed_at,
    {alias}.archived_at,
    {alias}.created_at,
    {alias}.updated_at
"""


def _issue_entity(row: asyncpg.Record) -> IssueEntity:
    """One issue row as a domain entity.

    A second copy of `IssueRepository._to_entity` rather than an import of
    it: that method is private to the class that owns `issues` reads, and
    reaching into it would make a change there silently a change here.

    The issue's columns are read under their bare names even in the two
    statements that return a relation and an issue side by side. Those
    disambiguate on the other side -- `relation_id`, `relation_created_at`
    -- precisely so this function needs no prefix argument to be passed
    correctly at every call site.
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


# One issue's relations in both directions, as a template with two holes.
#
# Two statement texts are built from it below rather than one text with a
# NULL-tolerant cursor predicate. That is not style: a parameter a statement
# never mentions has no inferable type, so `$3` and `$4` bound but unused
# would fail to parse outright -- and the obvious repair,
# `($3::TIMESTAMPTZ IS NULL OR (created_at, id) < ($3, $4))`, parses and then
# costs the index. The planner cannot turn an OR over a parameter into an
# index bound, so the keyset comparison degrades from a seek into a filter
# applied to every relation the issue has. Building two texts from one
# template keeps the union -- the part that is genuinely easy to get wrong --
# written once.
_RELATIONS_TEMPLATE = f"""
    SELECT
        edge.id AS relation_id,
        edge.type AS relation_type,
        edge.inverted AS relation_inverted,
        edge.created_at AS relation_created_at,
        {_ISSUE_COLUMNS.format(alias="other")}
    FROM (
        SELECT
            relation.id,
            relation.type,
            relation.created_at,
            relation.target_issue_id AS other_id,
            FALSE AS inverted
        FROM issue_relations AS relation
        WHERE relation.workspace_id = $1
            AND relation.source_issue_id = $2

        UNION ALL

        SELECT
            relation.id,
            relation.type,
            relation.created_at,
            relation.source_issue_id AS other_id,
            TRUE AS inverted
        FROM issue_relations AS relation
        WHERE relation.workspace_id = $1
            AND relation.target_issue_id = $2
    ) AS edge
    JOIN issues AS other
        ON other.workspace_id = $1 AND other.id = edge.other_id
    {{cursor}}
    ORDER BY edge.created_at DESC, edge.id DESC
    LIMIT ${{limit}}
"""

_RELATIONS_FIRST_PAGE = _RELATIONS_TEMPLATE.format(cursor="", limit=3)

_RELATIONS_AFTER_CURSOR = _RELATIONS_TEMPLATE.format(
    cursor="WHERE (edge.created_at, edge.id) < ($3, $4)",
    limit=5,
)


def _canonical(
    source_issue_id: UUID,
    target_issue_id: UUID,
    relation_type: RelationType,
) -> tuple[UUID, UUID, str]:
    """The single row that represents this relation, whichever way it is said.

    migrations/010_issue_relations.sql stores one row per relationship rather
    than one per direction, so every one of the four names a client may use
    has to be reduced to one of the three the table accepts, in the one order
    the table accepts it:

      * `blocked_by` is `blocks` with the ends exchanged -- the direction is
        the content, so it is preserved by swapping rather than by storing a
        second name;
      * `related` and `duplicate` have interchangeable ends, so they are
        stored lower id first. That is what makes `issue_relations_unique`
        refuse (B, A, related) after (A, B, related): the two are not two
        tuples the constraint has to be clever about, they are one tuple.
      * `blocks` in the order given, untouched.

    Returning the type as `str` rather than as a RelationType is not
    laziness. What comes back is a stored value, and only three of the four
    enum members are ones -- narrowing to `str` at the point the meaning
    changes is what stops a `BLOCKED_BY` being handed to an INSERT further
    down and refused by a CHECK constraint a long way from here.
    """
    if relation_type is RelationType.BLOCKED_BY:
        return target_issue_id, source_issue_id, RelationType.BLOCKS.value

    if relation_type in SYMMETRIC_TYPES and target_issue_id < source_issue_id:
        return target_issue_id, source_issue_id, relation_type.value

    return source_issue_id, target_issue_id, relation_type.value


class RelationRepository:
    """SQL access for the edges between issues.

    Two kinds of edge, one repository: `issues.parent_id` (the sub-issue
    edge) and the `issue_relations` table. They live together rather than
    with `IssueRepository` because what they have in common is being
    relationships -- every statement here is keyed on a PAIR of issues, and
    every one of them relies on the composite keys migration 010 adds. What
    they do not have in common with `IssueRepository` is a table.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class -- and neither
    does `asyncpg.PostgresError`: the constraint violations that are ordinary
    user input here are translated into domain errors before they leave,
    because deciding which client-supplied field a constraint name refers to
    requires knowing about a canonicalising swap that exists nowhere else.

    Every statement is scoped to one workspace, and the scope arrives as a
    required keyword argument, for the reasons `IssueRepository` sets out.
    """

    # --- sub-issues ---------------------------------------------------

    async def find_parent(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """This issue's parent, or nothing if it has none.

        One statement rather than "read the child, then read its parent".
        The join is what makes "the child does not exist", "the child has no
        parent" and "the child is in another workspace" one answer instead
        of three, and the middle one is the common case: a caller that had
        to tell them apart would be holding information the API does not
        expose anyway.

        The workspace predicate is applied to BOTH sides even though
        `issues_parent_fk` already guarantees they agree. It costs an
        equality against a column already in the index and it means this
        statement is correct on its own terms rather than correct because of
        a constraint declared in another file.
        """
        row = await connection.fetchrow(
            f"""
            SELECT {_ISSUE_COLUMNS.format(alias="parent")}
            FROM issues AS child
            JOIN issues AS parent
                ON parent.workspace_id = child.workspace_id
                AND parent.id = child.parent_id
            WHERE child.workspace_id = $1 AND child.id = $2
                AND parent.workspace_id = $1
            """,
            scope.workspace_id,
            issue_id,
        )

        if row is None:
            return None

        return _issue_entity(row)

    async def list_children(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        parent_id: UUID,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[IssueEntity]:
        """Keyset page of one issue's sub-issues, newest first.

        The same shape as `IssueRepository.list` and for the same reasons:
        `limit` is already `first + 1`, no OFFSET, and the tenant predicate
        is ANDed with the cursor rather than folded into the row-value
        comparison, so a page walk cannot fall out of the workspace or out
        of the parent.

        `parent_id` is an equality alongside `workspace_id`, which is what
        lets issues_workspace_parent_created_at_id_idx serve the whole
        statement -- both equalities are its leading columns and the cursor
        comparison is the rest of its key, in order.

        No team predicate anywhere: a sub-issue may belong to a different
        team than its parent, so filtering by the parent's team here would
        hide exactly the rows the product rule exists to allow.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                f"""
                SELECT {_ISSUE_COLUMNS.format(alias="child")}
                FROM issues AS child
                WHERE child.workspace_id = $1 AND child.parent_id = $2
                ORDER BY child.created_at DESC, child.id DESC
                LIMIT $3
                """,
                scope.workspace_id,
                parent_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                f"""
                SELECT {_ISSUE_COLUMNS.format(alias="child")}
                FROM issues AS child
                WHERE child.workspace_id = $1 AND child.parent_id = $2
                    AND (child.created_at, child.id) < ($3, $4)
                ORDER BY child.created_at DESC, child.id DESC
                LIMIT $5
                """,
                scope.workspace_id,
                parent_id,
                after_created_at,
                after_id,
                limit,
            )

        return [_issue_entity(row) for row in rows]

    async def lock_parenting(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
    ) -> None:
        """Serialise sub-issue re-parenting within one workspace.

        This is the whole of the multi-level cycle guard's soundness, so it
        is worth being precise about what it does.

        `is_ancestor_or_self` below reads an ancestry and `set_parent`
        changes one; between the two, another transaction re-parenting a
        different issue could invalidate what the first one read, and the
        two writes together would form a cycle neither could see on its own.
        A lock held across both closes that, and an advisory lock is what
        this needs rather than row locks: the rows that would have to be
        locked are the ones the walk has not reached yet.

        The lock is per workspace, not global, so tenants do not queue behind
        each other; and `_xact_`, so the caller's commit or rollback releases
        it with no cleanup path that could be skipped. `hashtext` collisions
        between two workspaces cost a little extra serialisation and cannot
        cost correctness.

        This is NOT a substitute for a database constraint and does not
        pretend to be one. It binds only callers that take it. Any future
        code that writes `issues.parent_id` -- a bulk import, an issueCreate
        that accepts a parent, an operator's UPDATE -- can still write a
        cycle, and nothing here will notice.
        """
        await connection.execute(
            "SELECT pg_advisory_xact_lock($1, hashtext($2))",
            PARENTING_LOCK_CLASS,
            str(scope.workspace_id),
        )

    async def is_ancestor_or_self(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        candidate_id: UUID,
    ) -> bool:
        """Whether `candidate_id` is `issue_id` or any of its ancestors.

        Asked of a proposed parent before it is written: if the issue being
        re-parented already appears above its proposed parent, attaching
        them closes a loop. Walking up from the parent rather than down from
        the child is what keeps the cost proportional to tree DEPTH instead
        of to sub-tree size.

        `CYCLE id SET is_cycle USING path` is not decoration. A recursive
        term over a table that already contained a cycle would not return a
        wrong answer, it would never return at all, and the one moment this
        query runs is the moment someone is trying to create one. The clause
        makes PostgreSQL stop at the first repeated id, so a database whose
        invariant has already been broken by some other writer produces an
        answer -- and, because the repeated id is still emitted once, an
        answer that still refuses the write.

        The workspace predicate is on the recursive term as well as on the
        anchor. Without it the walk would leave the tenant the moment it
        touched a row whose parent was written before migration 010's
        foreign key existed.
        """
        found: bool = await connection.fetchval(
            """
            WITH RECURSIVE ancestors (id, parent_id) AS (
                SELECT id, parent_id
                FROM issues
                WHERE workspace_id = $1 AND id = $2

                UNION ALL

                SELECT issues.id, issues.parent_id
                FROM issues
                JOIN ancestors ON issues.id = ancestors.parent_id
                WHERE issues.workspace_id = $1
            )
            CYCLE id SET is_cycle USING path
            SELECT EXISTS (SELECT 1 FROM ancestors WHERE id = $3)
            """,
            scope.workspace_id,
            issue_id,
            candidate_id,
        )

        return found

    async def set_parent(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        parent_id: UUID,
    ) -> IssueEntity | None:
        """Attach one issue to a parent; None if the issue is not here.

        The parent is not checked before the write. `issues_parent_fk` is a
        composite key onto `issues (workspace_id, id)`, so a parent in
        another workspace has no matching row and the server refuses this
        statement -- and a SELECT here first would be a second, weaker copy
        of that rule, weaker because the parent could be deleted between the
        two statements and weaker because it would then be two places that
        have to agree. That refusal is translated into
        RelatedIssueNotFoundError rather than propagated, because both ids
        came from the client: a client naming an issue that is not here is
        ordinary input, unlike `IssueRepository.create`, whose team id comes
        from the server and whose violation is therefore a defect.

        No row is a different answer from a refused row, and the difference
        is which id was wrong. Zero rows updated means the issue is not in
        this workspace, and the foreign key never ran because nothing
        changed.

        `updated_at` is written explicitly. Re-parenting is a modification of
        this row and its timestamp has to say so; the trigger that will
        eventually maintain the column does not exist yet, and when it does,
        setting the same value here is harmless.
        """
        try:
            row = await connection.fetchrow(
                f"""
                UPDATE issues AS child
                SET parent_id = $3,
                    updated_at = now()
                WHERE child.workspace_id = $1 AND child.id = $2
                RETURNING {_ISSUE_COLUMNS.format(alias="child")}
                """,
                scope.workspace_id,
                issue_id,
                parent_id,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            if exc.constraint_name != "issues_parent_fk":
                raise

            raise RelatedIssueNotFoundError(RelationEndpoint.PARENT) from None

        if row is None:
            return None

        return _issue_entity(row)

    async def clear_parent(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """Detach one issue from its parent; None if the issue is not here.

        Not guarded by `lock_parenting` and not a candidate for it: removing
        an edge cannot close a loop, and taking the workspace lock to do
        something that is safe in any interleaving would serialise a common
        operation for no guarantee.

        Clearing a parent an issue does not have is a successful no-op rather
        than an error. The caller asked for a state, the state holds, and
        reporting a failure would make a retry after a dropped response look
        like a different outcome from the first attempt.
        """
        row = await connection.fetchrow(
            f"""
            UPDATE issues AS child
            SET parent_id = NULL,
                updated_at = now()
            WHERE child.workspace_id = $1 AND child.id = $2
            RETURNING {_ISSUE_COLUMNS.format(alias="child")}
            """,
            scope.workspace_id,
            issue_id,
        )

        if row is None:
            return None

        return _issue_entity(row)

    # --- relations -----------------------------------------------------

    async def create_relation(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        source_issue_id: UUID,
        target_issue_id: UUID,
        relation_type: RelationType,
    ) -> IssueRelationEntity:
        """Record one relation, and return it from the source's point of view.

        The insert writes the canonical row; the SELECT that comes back with
        it reads the issue the CALLER called the target, which after
        canonicalisation may be either stored column. Both in one statement
        so that a concurrent delete cannot land between writing the row and
        reading its far end and turn a successful create into a missing
        issue.

        Neither issue is checked before the insert, for the reason
        `set_parent` gives: the two composite foreign keys are the check, and
        a SELECT first would be a rule stated twice with a gap in the middle.
        Both failures a client can cause are translated -- a missing issue
        and a relation that already exists -- and any other violation
        propagates, because it means the canonicalisation above is wrong and
        that is a defect, not input.
        """
        stored_source, stored_target, stored_type = _canonical(
            source_issue_id,
            target_issue_id,
            relation_type,
        )

        try:
            row = await connection.fetchrow(
                f"""
                WITH inserted AS (
                    INSERT INTO issue_relations (
                        workspace_id,
                        source_issue_id,
                        target_issue_id,
                        type
                    )
                    VALUES ($1, $2, $3, $4)
                    RETURNING id, created_at
                )
                SELECT
                    inserted.id AS relation_id,
                    inserted.created_at AS relation_created_at,
                    {_ISSUE_COLUMNS.format(alias="other")}
                FROM inserted
                JOIN issues AS other
                    ON other.workspace_id = $1 AND other.id = $5
                """,
                scope.workspace_id,
                stored_source,
                stored_target,
                stored_type,
                target_issue_id,
            )
        except asyncpg.ForeignKeyViolationError as exc:
            raise RelatedIssueNotFoundError(
                self._failing_endpoint(
                    exc.constraint_name,
                    swapped=stored_source != source_issue_id,
                )
            ) from None
        except asyncpg.UniqueViolationError as exc:
            if exc.constraint_name != "issue_relations_unique":
                raise

            raise DuplicateRelationError() from None

        return IssueRelationEntity(
            id=row["relation_id"],
            type=relation_type,
            issue=_issue_entity(row),
            created_at=row["relation_created_at"],
        )

    @staticmethod
    def _failing_endpoint(
        constraint_name: str | None,
        *,
        swapped: bool,
    ) -> RelationEndpoint:
        """Which end the CALLER named, given which stored key refused.

        `_canonical` may have exchanged the two ids, in which case the key
        named `..._source_fk` guards the issue the caller called the target.
        Reporting the constraint's own vocabulary would attach the error to
        the field the client got right.

        An unrecognised constraint name resolves to TARGET rather than
        raising. Only two foreign keys exist on this table and both are
        handled, so this branch is unreachable through the schema as
        written; making it a raise would convert a future third key into a
        masked internal error instead of a slightly mislabelled field.
        """
        if constraint_name == "issue_relations_source_fk":
            return RelationEndpoint.TARGET if swapped else RelationEndpoint.SOURCE

        return RelationEndpoint.SOURCE if swapped else RelationEndpoint.TARGET

    async def delete_relation(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        relation_id: UUID,
    ) -> UUID | None:
        """Remove one relation, or report that this workspace has no such id.

        The workspace is part of the predicate, not a check on the row
        afterwards, so a relation belonging to another tenant produces
        exactly the answer an id that exists nowhere produces. Deleting
        first and comparing `workspace_id` in the RETURNING would give the
        same None having already destroyed another tenant's row.

        One row at most: `id` is the primary key.
        """
        deleted: UUID | None = await connection.fetchval(
            """
            DELETE FROM issue_relations
            WHERE workspace_id = $1 AND id = $2
            RETURNING id
            """,
            scope.workspace_id,
            relation_id,
        )

        return deleted

    async def list_relations(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[IssueRelationEntity]:
        """Keyset page of one issue's relations, newest first, both directions.

        A UNION ALL of two equality lookups rather than
        `WHERE source = $2 OR target = $2`. The OR form is one statement and
        cannot be served by one index -- the two columns lead two different
        indexes -- so it degrades to a scan of the workspace's relations. The
        union lets each half land on
        issue_relations_workspace_source_created_at_id_idx and
        issue_relations_workspace_target_created_at_id_idx respectively, each
        already in the cursor's order.

        `inverted` records which half a row came from, and is what turns one
        stored row into the two readings the product needs: a `blocks` row
        found through its target column is a `BLOCKED_BY` to the issue that
        found it. UNION ALL, not UNION: `issue_relations_not_self` makes the
        two halves disjoint, so deduplicating would be a sort over the whole
        result to remove nothing.

        The join to `issues` is what makes this one round trip instead of a
        page of ids followed by a second query for their titles.

        ------------------------------------------------------------------
        FOR WHOEVER FIRST TRAVERSES THE BLOCKING GRAPH
        ------------------------------------------------------------------

        `blocks` is the only relation type exempt from canonical ordering --
        its direction is its content, so it cannot be reordered -- which
        means A-blocks-B and B-blocks-A are two genuinely distinct rows and
        the schema accepts both. Nothing refuses that pair, deliberately:
        mutual blocking is a workflow mistake, not an integrity violation.

        Nothing traverses today, so nothing can loop on it. This method is a
        single non-recursive UNION over ONE issue, and `Issue.relations`
        returns `IssueSummary`, which has no edges -- so the schema is
        acyclic by construction and a client cannot walk A -> B -> A either.

        The first thing that DOES traverse -- a dependency view, a
        topological sort, a "what is blocking this, recursively" query --
        must carry its own cycle guard, because nothing upstream of it
        provides one.

        The sub-issue tree is NOT affected, and the distinction is the part
        most likely to be lost: parenting IS cycle-guarded on write, by
        `RelationService.set_parent`. "We already prevent cycles" is true of
        parenting and false of blocking, and assuming it covers both is the
        mistake this paragraph exists to stop.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                _RELATIONS_FIRST_PAGE,
                scope.workspace_id,
                issue_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                _RELATIONS_AFTER_CURSOR,
                scope.workspace_id,
                issue_id,
                after_created_at,
                after_id,
                limit,
            )

        return [
            IssueRelationEntity(
                id=row["relation_id"],
                type=(
                    invert(RelationType(row["relation_type"]))
                    if row["relation_inverted"]
                    else RelationType(row["relation_type"])
                ),
                issue=_issue_entity(row),
                created_at=row["relation_created_at"],
            )
            for row in rows
        ]
