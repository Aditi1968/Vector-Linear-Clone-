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


# The two foreign keys migrations/009_projects.sql puts on `issues`, and the
# field error each one means. Keyed on the constraint name because both raise
# the same exception class and point a client at different halves of its
# request.
#
# Neither distinguishes "belongs to another workspace" from "does not exist":
# a project in another tenant breaks `issues_project_fk` exactly as a
# nonexistent id does, and both answer NOT_FOUND. That is the point -- telling
# them apart would confirm the existence of a resource the caller cannot see.
PROJECT_CONSTRAINT_ERRORS: dict[str, ValidationIssue] = {
    "issues_project_fk": ValidationIssue(
        field="projectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    "issues_milestone_fk": ValidationIssue(
        field="milestoneId",
        code="NOT_FOUND",
        message="Milestone not found in this project",
    ),
}

ISSUE_NOT_FOUND = ValidationIssue(
    field="issueId",
    code="NOT_FOUND",
    message="Issue not found",
)


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

    async def set_project(
        self,
        *,
        scope: WorkspaceScope,
        issue_id: UUID,
        project_id: UUID | None,
        milestone_id: UUID | None,
    ) -> IssueEntity:
        """Move one issue into a project and milestone, or out of both.

        Both are supplied together, and passing None for both is how an issue
        leaves a project. There is no separate "clear" operation because there
        is no separate state: an issue's place in the plan is one pair of
        values, and the schema refuses three of the four combinations of
        present and absent.

        The one combination this can rule out without asking the database --
        a milestone with no project -- is checked here, because it is a
        property of the arguments alone. Nothing else is: whether the project
        is in this workspace, and whether the milestone belongs to that
        project, are facts about rows that can change between a check and a
        write, so they are left to `issues_project_fk` and
        `issues_milestone_fk` and translated from the constraint they name.

        `issues_milestone_requires_project` is deliberately absent from that
        translation. If the pre-check above is right, the constraint cannot
        fire; if it ever does, the two disagree, and that is a defect to
        surface as one rather than to report to a client as bad input.
        """
        self._validate_set_project(project_id=project_id, milestone_id=milestone_id)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    issue = await self._repository.set_project(
                        connection,
                        scope=scope,
                        issue_id=issue_id,
                        project_id=project_id,
                        milestone_id=milestone_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    mapped = PROJECT_CONSTRAINT_ERRORS.get(error.constraint_name or "")

                    if mapped is None:
                        raise

                    raise ValidationError([mapped]) from None

        if issue is None:
            raise ValidationError([ISSUE_NOT_FOUND])

        return issue

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
    def _validate_set_project(
        *,
        project_id: UUID | None,
        milestone_id: UUID | None,
    ) -> None:
        """The one thing about this operation the arguments alone decide.

        A milestone belongs to a project; asking for one without the other is
        not a lookup that might succeed, it is a request that cannot be
        satisfied by any state of the database. Rejecting it here means no
        connection is acquired for it, which is the same property
        `_validate_create` gives the create path.
        """
        if milestone_id is not None and project_id is None:
            raise ValidationError(
                [
                    ValidationIssue(
                        field="milestoneId",
                        code="PROJECT_REQUIRED",
                        message="A milestone can only be set together with its project",
                    )
                ]
            )

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
