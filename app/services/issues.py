from uuid import UUID

import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.issues import IssueEntity
from app.domain.pagination import (
    InvalidCursorError,
    IssueCursor,
    IssuePage,
    decode_issue_cursor,
    encode_issue_cursor,
)
from app.repositories.issues import IssueRepository


TITLE_MIN_LENGTH = 1
TITLE_MAX_LENGTH = 500

PRIORITY_MIN = 0
PRIORITY_MAX = 4

FIRST_MIN = 1
FIRST_MAX = 100


class IssueService:
    """Business rules for issues.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service
    also owns connection acquisition and transaction boundaries.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: IssueRepository,
    ):
        self._pool = pool
        self._repository = repository

    async def get_by_id(
        self,
        issue_id: UUID,
    ) -> IssueEntity | None:
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(connection, issue_id)

    async def create(
        self,
        *,
        title: str,
        description: str | None,
        priority: int,
    ) -> IssueEntity:
        self._validate_create(title=title, priority=priority)

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block
            # will also carry the audit / sync / outbox writes.
            async with connection.transaction():
                return await self._repository.create(
                    connection,
                    title=title,
                    description=description,
                    priority=priority,
                )

    async def list(
        self,
        *,
        first: int,
        after: str | None,
    ) -> IssuePage:
        """Forward keyset page of issues, newest first.

        A single SELECT needs no explicit write transaction, so this
        acquires a connection without opening one.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list(
                connection,
                limit=first + 1,
                after_created_at=cursor.created_at if cursor is not None else None,
                after_id=cursor.id if cursor is not None else None,
            )

        has_next_page = len(rows) > first
        nodes = rows[:first]

        end_cursor = None

        if nodes:
            # Built from the last RETURNED node, never from the extra row.
            last = nodes[-1]
            end_cursor = encode_issue_cursor(last.created_at, last.id)

        return IssuePage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    @staticmethod
    def _validate_list(*, first: int, after: str | None) -> IssueCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an
        expected input error rather than a parser exception.
        """
        issues: list[ValidationIssue] = []
        cursor: IssueCursor | None = None

        if first < FIRST_MIN or first > FIRST_MAX:
            issues.append(
                ValidationIssue(
                    field="first",
                    code="OUT_OF_RANGE",
                    message=f"first must be between {FIRST_MIN} and {FIRST_MAX}",
                )
            )

        if after is not None:
            try:
                cursor = decode_issue_cursor(after)
            except InvalidCursorError:
                issues.append(
                    ValidationIssue(
                        field="after",
                        code="INVALID_CURSOR",
                        message="Cursor is invalid",
                    )
                )

        if issues:
            raise ValidationError(issues)

        return cursor

    @staticmethod
    def _validate_create(*, title: str, priority: int) -> None:
        """Collect every violation, then raise once.

        Field order is deterministic (title, then priority) so that clients
        can rely on it. The codes and messages are a public contract.
        """
        issues: list[ValidationIssue] = []

        # The title is validated as supplied -- never trimmed or rewritten.
        if len(title) < TITLE_MIN_LENGTH:
            issues.append(
                ValidationIssue(
                    field="title",
                    code="REQUIRED",
                    message="Title is required",
                )
            )
        elif len(title) > TITLE_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="title",
                    code="TOO_LONG",
                    message=f"Title must be at most {TITLE_MAX_LENGTH} characters",
                )
            )

        if priority < PRIORITY_MIN or priority > PRIORITY_MAX:
            issues.append(
                ValidationIssue(
                    field="priority",
                    code="OUT_OF_RANGE",
                    message=(
                        f"Priority must be between {PRIORITY_MIN} and {PRIORITY_MAX}"
                    ),
                )
            )

        if issues:
            raise ValidationError(issues)
