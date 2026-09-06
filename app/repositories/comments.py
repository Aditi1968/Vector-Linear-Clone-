from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.comments import CommentEntity
from app.domain.tenancy import WorkspaceScope


class CommentRepository:
    """SQL access for `comments`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.

    Every statement is scoped to one workspace and the scope arrives as a
    required keyword argument. That is what makes a read of another tenant's
    issue return nothing rather than that tenant's discussion: `workspace_id`
    is an equality in the WHERE clause, not a filter applied to rows already
    fetched.

    `create` does not check that the issue belongs to the workspace.
    `comments_issue_fk` is composite against `issues (workspace_id, id)`, so
    the server refuses a mismatched pair as part of the insert -- see
    `IssueRepository.create` for why a SELECT first would be a second, weaker
    copy of that rule rather than a safety net over it.
    """

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        author_id: UUID,
        body: str,
    ) -> CommentEntity:
        """Write one comment onto one issue in this workspace.

        `edited_at` is left NULL, which is the claim "never edited". It is not
        set to `created_at`: a comment written and never touched has no edit
        to report, and seeding the column would make "has this been edited"
        unanswerable from the row.
        """
        row = await connection.fetchrow(
            """
            INSERT INTO comments (workspace_id, issue_id, author_id, body)
            VALUES ($1, $2, $3, $4)
            RETURNING
                id,
                issue_id,
                author_id,
                body,
                edited_at,
                created_at,
                updated_at
            """,
            scope.workspace_id,
            issue_id,
            author_id,
            body,
        )

        return self._to_entity(row)

    async def delete(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        comment_id: UUID,
        author_id: UUID,
    ) -> bool:
        """Remove one of this author's comments; True if a row went.

        A hard delete, not a `deleted_at` flag. A comment is what a person
        wrote and asked to withdraw; the durable record of what happened to an
        issue is an activity log with an explicit event type, which is a
        separate table this phase does not create. Keeping tombstones here
        would put half an audit trail in the discussion, where every later
        read has to remember to exclude it.

        Both the workspace and the author are equalities in the WHERE clause,
        never a check applied to a row already read. A comment in another
        tenant, a comment by another author, and an id that exists nowhere all
        delete nothing and answer False -- one answer, reached without this
        process ever loading a row it was not entitled to.
        """
        deleted = await connection.fetchval(
            """
            DELETE FROM comments
            WHERE workspace_id = $1 AND id = $2 AND author_id = $3
            RETURNING id
            """,
            scope.workspace_id,
            comment_id,
            author_id,
        )

        return deleted is not None

    async def list_for_issue(
        self,
        connection: asyncpg.Connection,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[CommentEntity]:
        """Keyset page of one issue's comments, oldest first.

        `limit` is expected to already be `first + 1` so the caller can detect
        a following page. No OFFSET: the cursor is a row-value comparison, and
        (workspace_id, issue_id) leads both statements so a page is served by
        the leading columns of `comments_workspace_issue_created_idx`.

        Ascending, unlike issues: a discussion reads forwards, and a reader
        arriving at an issue wants the first thing said, not the last.

        Both equalities are ANDed with the cursor rather than folded into it.
        A row-value comparison widened to include workspace_id or issue_id
        would put them into the ORDER BY, which is how a page walk leaves one
        issue -- or one tenant -- and continues into whichever sorts next.

        An issue in another workspace is not a separate case: the workspace
        equality already excludes every one of its comments, so the page comes
        back empty rather than holding somebody else's thread.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    issue_id,
                    author_id,
                    body,
                    edited_at,
                    created_at,
                    updated_at
                FROM comments
                WHERE workspace_id = $1 AND issue_id = $2
                ORDER BY created_at, id
                LIMIT $3
                """,
                scope.workspace_id,
                issue_id,
                limit,
            )
        else:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    issue_id,
                    author_id,
                    body,
                    edited_at,
                    created_at,
                    updated_at
                FROM comments
                WHERE workspace_id = $1
                    AND issue_id = $2
                    AND (created_at, id) > ($3, $4)
                ORDER BY created_at, id
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
    def _to_entity(row: asyncpg.Record) -> CommentEntity:
        return CommentEntity(
            id=row["id"],
            issue_id=row["issue_id"],
            author_id=row["author_id"],
            body=row["body"],
            edited_at=row["edited_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
