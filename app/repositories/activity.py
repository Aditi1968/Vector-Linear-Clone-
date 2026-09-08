from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.activity import ActivityEntity, ActivityKind
from app.domain.tenancy import WorkspaceScope


# Every column an ActivityEntity is built from, as one expression list shared
# by the statements below.
#
# Interpolated with an f-string, which is a rule about VALUES rather than
# about text: this is a module-level literal no input can influence, and the
# alternative is two copies of the same seven lines that drift the day a
# column is added to one of them. Every actual value below arrives as $n.
_ACTIVITY_COLUMNS = """
                id,
                issue_id,
                actor_id,
                kind,
                from_value,
                to_value,
                caused_by,
                created_at
"""


class ActivityRepository:
    """SQL access for `issue_activity`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Taking the connection is what makes the history atomic with the change it
    describes: the service that changes an issue is already inside a
    transaction, hands that same connection here, and the two writes commit or
    roll back together. A repository that acquired its own connection would
    write history for changes that were then rolled back -- and, worse, would
    do it invisibly.

    Append-only. There is no update and no delete here, deliberately: a
    history that can be rewritten is a history nobody can rely on, and the
    absence of the statement is a stronger guarantee than a rule about not
    calling it.
    """

    async def record(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        actor_id: UUID | None,
        kind: ActivityKind,
        from_value: str | None = None,
        to_value: str | None = None,
        caused_by: str | None = None,
    ) -> None:
        """Append one event to one issue's history.

        Nothing is returned. A history row is written for the sake of later
        reads and no caller has anything to do with it now; handing one back
        would invite a resolver to report it, which is how an internal record
        becomes an accidental part of a mutation's contract.

        The issue is not checked against the workspace first.
        `issue_activity_issue_fk` is composite against
        `issues (workspace_id, id)`, so a mismatched pair is refused as part
        of this statement -- and since the caller has just written to that
        same issue under the same scope, a violation here is a defect rather
        than user input, and propagates as one.

        `kind` is bound as the StrEnum member. asyncpg sends it as its string
        value, which is the spelling `issue_activity_kind_known` admits, so a
        kind this application does not know cannot be spelled at all.

        `caused_by` defaults to None and is None for every write a person made:
        the actor is the cause, and repeating it in a second column would be a
        second place for the two to disagree. It is written only where
        `actor_id` is None AND something other than nobody did it -- today,
        exactly the GitHub status automation. See migration 030.
        """
        await connection.execute(
            """
            INSERT INTO issue_activity (
                workspace_id,
                issue_id,
                actor_id,
                kind,
                from_value,
                to_value,
                caused_by
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            scope.workspace_id,
            issue_id,
            actor_id,
            kind.value,
            from_value,
            to_value,
            caused_by,
        )

    async def list_for_issue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[ActivityEntity]:
        """Keyset page of one issue's history, newest first.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison, and
        (workspace_id, issue_id) leads both statements so a page is served by
        the leading columns of
        `issue_activity_workspace_issue_created_idx`.

        Both equalities are ANDed with the cursor rather than folded into it.
        A row-value comparison widened to include workspace_id or issue_id
        would put them into the ORDER BY, which is how a page walk leaves one
        issue -- or one tenant -- and continues into whichever sorts next.

        An issue in another workspace is not a separate case: the workspace
        equality already excludes every one of its rows, so the page comes
        back empty rather than holding somebody else's history.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                f"""
                SELECT
{_ACTIVITY_COLUMNS}
                FROM issue_activity
                WHERE workspace_id = $1 AND issue_id = $2
                ORDER BY created_at DESC, id DESC
                LIMIT $3
                """,
                scope.workspace_id,
                issue_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                f"""
                SELECT
{_ACTIVITY_COLUMNS}
                FROM issue_activity
                WHERE workspace_id = $1
                    AND issue_id = $2
                    AND (created_at, id) < ($3, $4)
                ORDER BY created_at DESC, id DESC
                LIMIT $5
                """,
                scope.workspace_id,
                issue_id,
                after_created_at,
                after_id,
                limit,
            )

        return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> ActivityEntity:
        return ActivityEntity(
            id=row["id"],
            issue_id=row["issue_id"],
            actor_id=row["actor_id"],
            # Converted here rather than carried as text, so an unknown kind
            # -- which the CHECK makes unwritable, and which a hand-edited row
            # could still produce -- fails in the repository that read it
            # rather than in whatever renders it three layers up.
            kind=ActivityKind(row["kind"]),
            from_value=row["from_value"],
            to_value=row["to_value"],
            caused_by=row["caused_by"],
            created_at=row["created_at"],
        )
