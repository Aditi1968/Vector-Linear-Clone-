from datetime import datetime
from uuid import UUID

import asyncpg

from app.domain.issues import IssueEntity


class IssueRepository:
    """SQL access for `issues`.

    The repository receives a connection from the service layer. It never
    acquires connections, never touches the pool, and never owns a
    transaction. `asyncpg.Record` never escapes this class.
    """

    async def get_by_id(
        self,
        connection: asyncpg.Connection,
        issue_id: UUID,
    ) -> IssueEntity | None:
        row = await connection.fetchrow(
            """
            SELECT
                id,
                title,
                description,
                priority,
                completed_at,
                created_at,
                updated_at
            FROM issues
            WHERE id = $1
            """,
            issue_id,
        )

        if row is None:
            return None

        return self._to_entity(row)

    async def create(
        self,
        connection: asyncpg.Connection,
        *,
        title: str,
        description: str | None,
        priority: int,
    ) -> IssueEntity:
        row = await connection.fetchrow(
            """
            INSERT INTO issues (
                title,
                description,
                priority
            )
            VALUES ($1, $2, $3)
            RETURNING
                id,
                title,
                description,
                priority,
                completed_at,
                created_at,
                updated_at
            """,
            title,
            description,
            priority,
        )

        return self._to_entity(row)

    async def list(
        self,
        connection: asyncpg.Connection,
        *,
        limit: int,
        after_created_at: datetime | None,
        after_id: UUID | None,
    ) -> list[IssueEntity]:
        """Keyset page of issues, newest first.

        `limit` is expected to already be `first + 1` so the caller can
        detect a following page. No OFFSET: the cursor is a row-value
        comparison matching issues_created_at_id_idx.
        """
        if after_created_at is None or after_id is None:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    title,
                    description,
                    priority,
                    completed_at,
                    created_at,
                    updated_at
                FROM issues
                ORDER BY created_at DESC, id DESC
                LIMIT $1
                """,
                limit,
            )
        else:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    title,
                    description,
                    priority,
                    completed_at,
                    created_at,
                    updated_at
                FROM issues
                WHERE (created_at, id) < ($1, $2)
                ORDER BY created_at DESC, id DESC
                LIMIT $3
                """,
                after_created_at,
                after_id,
                limit,
            )

        return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: asyncpg.Record) -> IssueEntity:
        return IssueEntity(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            priority=row["priority"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
