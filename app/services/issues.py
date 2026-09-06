from typing import TYPE_CHECKING
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
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository


if TYPE_CHECKING:
    from app.services.teams import TeamService


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

    The workspace is threaded through as an argument on every method rather
    than held on the instance. A service constructed per request could hold
    one and still be correct today, but the moment anything caches, shares
    or reuses a service -- a worker looping over tenants, a batch job, a
    module-level singleton -- an instance attribute becomes an ambient
    current workspace that the next operation inherits without asking. See
    WorkspaceScope on why tenant identity has to travel with the operation.

    Holding a scope is not permission to act in it. Nothing in this class
    checks that the caller belongs to the workspace it named; that check
    does not exist yet, and when it does it will not live here.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: IssueRepository,
        teams: "TeamService",
    ):
        self._pool = pool
        self._repository = repository

        # Creating an issue needs two things only the team layer can answer:
        # the next number on that team's counter, and the state a new issue
        # starts in. Both have to be decided inside this service's
        # transaction, so the collaborator is a service rather than a
        # repository -- see `TeamService.allocate_issue_number` for why the
        # allocation cannot own a transaction of its own.
        #
        # Required, not defaulted. A default would let a context be built
        # whose `create` fails at the first attempt to file an issue rather
        # than at construction -- and the tests that never create would go
        # on passing, which is exactly how the missing number column reached
        # an integrated database in the first place.
        self._teams = teams

    async def get_by_id(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
    ) -> IssueEntity | None:
        """One issue from this workspace, or nothing.

        "Not in this workspace" and "does not exist" are the same answer on
        purpose; the repository explains why the distinction must not be
        observable.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(
                connection,
                scope=scope,
                issue_id=issue_id,
            )

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        team_id: UUID,
        title: str,
        description: str | None,
        priority: int,
    ) -> IssueEntity:
        """File one issue in this workspace, against this team.

        The team is a required argument rather than something resolved in
        here. A service that picked a default team would be choosing where
        another tenant's work lands, using a rule invisible at the call
        site; the caller that knows which team it means is the one that has
        to say so.
        """
        self._validate_create(title=title, priority=priority)

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block
            # will also carry the audit / sync / outbox writes.
            async with connection.transaction():
                # Resolved before the number is claimed, deliberately. The
                # allocation takes a row lock on the team that is held to
                # the end of this transaction and serialises every other
                # creation on the same team for that whole span, so the
                # read that does not need the lock happens outside it.
                workflow_state_id = await self._teams.default_workflow_state_id(
                    connection,
                    scope=scope,
                    team_id=team_id,
                )

                number = await self._teams.allocate_issue_number(
                    connection,
                    scope=scope,
                    team_id=team_id,
                )

                return await self._repository.create(
                    connection,
                    scope=scope,
                    team_id=team_id,
                    number=number,
                    workflow_state_id=workflow_state_id,
                    title=title,
                    description=description,
                    priority=priority,
                )

    async def list(
        self,
        *,
        scope: WorkspaceScope,
        first: int,
        after: str | None,
    ) -> IssuePage:
        """Forward keyset page of one workspace's issues, newest first.

        A single SELECT needs no explicit write transaction, so this
        acquires a connection without opening one.

        The cursor is not trusted to carry a workspace and could not be if
        it did: it is Base64 over JSON, readable and writable by anyone
        holding it. The scope comes from this call, so a cursor minted in
        one workspace and replayed against another selects nothing rather
        than resuming someone else's page.
        """
        cursor = self._validate_list(first=first, after=after)

        async with self._pool.acquire() as connection:
            # One extra row tells us whether a further page exists.
            rows = await self._repository.list(
                connection,
                scope=scope,
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
