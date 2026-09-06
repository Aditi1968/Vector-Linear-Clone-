from datetime import date
from typing import NoReturn
from uuid import UUID

import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.pagination import (
    InvalidCursorError,
    KeysetCursor,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from app.domain.patch import UNSET, UnsetType
from app.domain.projects import (
    PROJECT_STATES,
    ProjectEntity,
    ProjectMilestoneEntity,
    ProjectPage,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository


NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 200

DESCRIPTION_MAX_LENGTH = 10_000

POSITION_MIN = 0

FIRST_MIN = 1
FIRST_MAX = 100


# Constraint name -> the field error it means, for the violations that are
# ordinary consequences of client input rather than defects.
#
# The mapping is keyed on the constraint name and not on the exception class,
# because two constraints on one statement raise the same class and mean
# entirely different things: on `project_teams` a foreign key failure is either
# "no such project" or "no such team", and reporting the wrong one sends a
# client to correct the wrong half of its request.
#
# What is deliberately NOT distinguished is the tenant. A team from another
# workspace and a team that does not exist both break
# `project_teams_team_fk`, so both arrive here and both produce the same
# NOT_FOUND. Telling them apart would require a lookup this service does not
# perform, and performing it would answer a question -- "does this id exist
# somewhere I cannot see" -- that no client may be allowed to ask.
_CONSTRAINT_ERRORS: dict[str, ValidationIssue] = {
    "project_teams_project_fk": ValidationIssue(
        field="projectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    "project_teams_team_fk": ValidationIssue(
        field="teamId",
        code="NOT_FOUND",
        message="Team not found",
    ),
    "project_teams_pkey": ValidationIssue(
        field="teamId",
        code="ALREADY_ASSOCIATED",
        message="Team is already on this project",
    ),
    "project_milestones_project_fk": ValidationIssue(
        field="projectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
}

_PROJECT_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Project not found",
)

_MILESTONE_NOT_FOUND = ValidationIssue(
    field="id",
    code="NOT_FOUND",
    message="Milestone not found",
)


def _raise_mapped(error: asyncpg.PostgresError) -> NoReturn:
    """Translate a named constraint violation, or re-raise it untouched.

    The re-raise is the important half. A violation this service did not
    anticipate is a defect -- a missing NOT NULL, a constraint added by a
    later migration nobody taught this mapping about -- and turning it into a
    field error would tell a client to fix its input for a problem that is not
    in the input, while hiding the defect behind a 200.
    """
    issue = _CONSTRAINT_ERRORS.get(error.constraint_name or "")

    if issue is None:
        raise error

    raise ValidationError([issue]) from None


class ProjectService:
    """Business rules for projects, their teams and their milestones.

    Validation lives here rather than in the GraphQL layer so that REST,
    workers and internal jobs all go through the same rules. The service also
    owns connection acquisition and transaction boundaries.

    The workspace is threaded through as an argument on every method rather
    than held on the instance, for the reason IssueService states: an instance
    attribute becomes an ambient current workspace that the next operation
    inherits without asking.

    Holding a scope is not permission to act in it. Nothing in this class
    checks that the caller belongs to the workspace it named, or may write to
    the project it named; those checks do not exist yet, and when they do they
    will not live here.

    ## Where tenancy is actually enforced

    Not in this file. Every cross-workspace association is refused by a
    composite foreign key in migrations/009_projects.sql, and this service's
    only job on that path is to turn the refusal into a message. There is no
    SELECT that reads a project to check its workspace before writing a row
    that references it: such a check is a second, weaker copy of the
    constraint -- a separate statement, so the row can change between the two,
    and a second place that has to agree with the first.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        repository: ProjectRepository,
        issue_repository: IssueRepository,
    ):
        self._pool = pool
        self._repository = repository

        # Deleting a project has to detach the issues that point at it, and
        # SQL against `issues` belongs to the repository that owns that table.
        # A service reaching across to a second repository inside one
        # transaction is the shape this architecture is for; a project
        # repository writing to `issues` would not be.
        self._issue_repository = issue_repository

    # ----------------------------------------------------------------- reads

    async def get_by_id(
        self,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> ProjectEntity | None:
        """One project from this workspace, or nothing.

        "Not in this workspace" and "does not exist" are the same answer on
        purpose; the repository explains why the distinction must not be
        observable.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.get_by_id(
                connection,
                scope=scope,
                project_id=project_id,
            )

    async def get_many_by_ids(
        self,
        *,
        scope: WorkspaceScope,
        project_ids: list[UUID],
    ) -> list[ProjectEntity]:
        """The projects from this workspace with these ids, in no order.

        Exists for batching -- see app/graphql/loaders/projects.py. An empty
        request is answered without a connection: a batch loader legitimately
        ends up with nothing to load, and paying a round trip to ask the
        server about an empty array is a cost with no answer attached.
        """
        if not project_ids:
            return []

        async with self._pool.acquire() as connection:
            return await self._repository.get_many_by_ids(
                connection,
                scope=scope,
                project_ids=project_ids,
            )

    async def list(
        self,
        *,
        scope: WorkspaceScope,
        first: int,
        after: str | None,
    ) -> ProjectPage:
        """Forward keyset page of one workspace's projects, newest first.

        A single SELECT needs no explicit write transaction, so this acquires
        a connection without opening one.

        The cursor is not trusted to carry a workspace and could not be if it
        did: it is Base64 over JSON, readable and writable by anyone holding
        it. The scope comes from this call, so a cursor minted in one
        workspace and replayed against another selects nothing rather than
        resuming someone else's page.
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
            end_cursor = encode_keyset_cursor(last.created_at, last.id)

        return ProjectPage(
            nodes=nodes,
            has_next_page=has_next_page,
            end_cursor=end_cursor,
        )

    async def list_milestones(
        self,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> list[ProjectMilestoneEntity]:
        """One project's milestones, in display order.

        A project in another workspace, and one that does not exist, both
        produce an empty list -- the same answer a real project with no
        milestones gives. Nothing here reveals which of the three it was.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list_milestones(
                connection,
                scope=scope,
                project_id=project_id,
            )

    async def list_milestones_for_projects(
        self,
        *,
        scope: WorkspaceScope,
        project_ids: list[UUID],
    ) -> list[ProjectMilestoneEntity]:
        """Several projects' milestones at once, for batching."""
        if not project_ids:
            return []

        async with self._pool.acquire() as connection:
            return await self._repository.list_milestones_for_projects(
                connection,
                scope=scope,
                project_ids=project_ids,
            )

    # --------------------------------------------------------------- project

    async def create(
        self,
        *,
        scope: WorkspaceScope,
        name: str,
        description: str | None,
        state: str,
        target_date: date | None,
    ) -> ProjectEntity:
        """Create one project in this workspace, associated with no teams yet.

        Teams are a separate call rather than a list on this one. Associating
        a team can fail on its own terms -- the team may not exist, or may
        belong to another workspace -- and folding those failures into
        creation would mean either abandoning the project over one bad team id
        or reporting a partial success that no payload shape describes well.
        """
        self._validate_project_fields(name=name, description=description, state=state)

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block
            # will also carry the audit / sync / outbox writes.
            async with connection.transaction():
                return await self._repository.create(
                    connection,
                    scope=scope,
                    name=name,
                    description=description,
                    state=state,
                    target_date=target_date,
                )

    async def update(
        self,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        name: str | UnsetType = UNSET,
        description: str | None | UnsetType = UNSET,
        state: str | UnsetType = UNSET,
        target_date: date | None | UnsetType = UNSET,
    ) -> ProjectEntity:
        """Apply a partial update and return the project as it now stands.

        `UNSET` and `None` are different arguments: UNSET leaves a field
        alone, None clears it. `name` and `state` cannot be cleared -- the
        columns are NOT NULL -- so their types admit no None at all, and a
        client that sends null for either is refused by the GraphQL layer
        before this is called.

        A patch that sets nothing does not reach the database. The UPDATE
        would be harmless except for `updated_at = now()`, and stamping a row
        as modified because a client sent an empty form is a lie that
        propagates into every "recently changed" view built on that column.
        """
        self._validate_project_fields(name=name, description=description, state=state)

        if (
            name is UNSET
            and description is UNSET
            and state is UNSET
            and target_date is UNSET
        ):
            existing = await self.get_by_id(scope=scope, project_id=project_id)

            if existing is None:
                raise ValidationError([_PROJECT_NOT_FOUND])

            return existing

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                updated = await self._repository.update(
                    connection,
                    scope=scope,
                    project_id=project_id,
                    set_name=name is not UNSET,
                    name=None if isinstance(name, UnsetType) else name,
                    set_description=description is not UNSET,
                    description=(
                        None if isinstance(description, UnsetType) else description
                    ),
                    set_state=state is not UNSET,
                    state=None if isinstance(state, UnsetType) else state,
                    set_target_date=target_date is not UNSET,
                    target_date=(
                        None if isinstance(target_date, UnsetType) else target_date
                    ),
                )

        if updated is None:
            raise ValidationError([_PROJECT_NOT_FOUND])

        return updated

    async def delete(self, *, scope: WorkspaceScope, project_id: UUID) -> None:
        """Delete one project and everything that hangs off it.

        The detachment is written out rather than delegated to ON DELETE
        CASCADE, and the order is the order the foreign keys require:

            issues -> milestones -> team links -> the project itself

        Two reasons for doing it here. The obvious one is that CASCADE would
        make `DELETE FROM projects WHERE id = ...` silently rewrite rows in
        three other tables, reporting `DELETE 1`; every foreign key onto
        `projects` is RESTRICT precisely so that cannot happen by accident.
        The less obvious one is that the two tables are not treated alike --
        milestones are destroyed, issues are kept and merely unassigned --
        and that is a product decision, which belongs in a service where it
        can be read and changed, not in a schema clause.

        All of it in one transaction, so a failure part way through leaves the
        project intact with its issues still attached rather than half
        dismantled.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._issue_repository.clear_project(
                    connection,
                    scope=scope,
                    project_id=project_id,
                )
                await self._repository.delete_milestones_for_project(
                    connection,
                    scope=scope,
                    project_id=project_id,
                )
                await self._repository.clear_teams(
                    connection,
                    scope=scope,
                    project_id=project_id,
                )

                deleted = await self._repository.delete(
                    connection,
                    scope=scope,
                    project_id=project_id,
                )

                if not deleted:
                    # Raised inside the transaction so it rolls back. Nothing
                    # above it could have matched a row -- the project is not
                    # in this workspace, so neither is anything referencing it
                    # -- but relying on that to leave the database untouched
                    # would be relying on an argument rather than on the
                    # rollback that makes it true.
                    raise ValidationError([_PROJECT_NOT_FOUND])

    # ----------------------------------------------------------------- teams

    async def add_team(
        self,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        team_id: UUID,
    ) -> ProjectEntity:
        """Put one team on one project, and return the project.

        This is the operation the whole feature exists for: a project spans
        teams, so this is called more than once per project and each call
        stands alone.

        The project and the team are never looked up first. `project_teams`
        holds one `workspace_id` for the row and both of its foreign keys read
        it, so PostgreSQL is what refuses a team from another workspace -- as
        one statement, with nothing in between for a concurrent move to
        exploit.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    await self._repository.add_team(
                        connection,
                        scope=scope,
                        project_id=project_id,
                        team_id=team_id,
                    )
                except (
                    asyncpg.ForeignKeyViolationError,
                    asyncpg.UniqueViolationError,
                ) as error:
                    _raise_mapped(error)

                project = await self._repository.get_by_id(
                    connection,
                    scope=scope,
                    project_id=project_id,
                )

        if project is None:
            # Unreachable through the constraint above -- the INSERT succeeded,
            # so a project with this id exists in this workspace -- but stated
            # rather than assumed, because the alternative is returning a
            # `ProjectEntity | None` from a method whose whole contract is that
            # it worked.
            raise ValidationError([_PROJECT_NOT_FOUND])

        return project

    async def remove_team(
        self,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        team_id: UUID,
    ) -> ProjectEntity:
        """Take one team off one project, and return the project.

        A team that was not on the project, a project in another workspace and
        a project that never existed are all answered the same way: the delete
        matches nothing, and the reload below decides between "here is the
        project" and NOT_FOUND. Removal is not reported as a failure when
        there was nothing to remove, because the caller's intent -- this team
        is not on this project -- already holds.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.remove_team(
                    connection,
                    scope=scope,
                    project_id=project_id,
                    team_id=team_id,
                )

                project = await self._repository.get_by_id(
                    connection,
                    scope=scope,
                    project_id=project_id,
                )

        if project is None:
            raise ValidationError([_PROJECT_NOT_FOUND])

        return project

    # ------------------------------------------------------------ milestones

    async def create_milestone(
        self,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        name: str,
        target_date: date | None,
    ) -> ProjectMilestoneEntity:
        """Append one milestone to a project.

        The project is not read first: `project_milestones_project_fk` is
        composite over the workspace, so a project from another tenant is
        refused by the same statement that would have written the row.
        """
        self._validate_milestone_fields(name=name)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    return await self._repository.create_milestone(
                        connection,
                        scope=scope,
                        project_id=project_id,
                        name=name,
                        target_date=target_date,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

    async def update_milestone(
        self,
        *,
        scope: WorkspaceScope,
        milestone_id: UUID,
        name: str | UnsetType = UNSET,
        target_date: date | None | UnsetType = UNSET,
        position: int | UnsetType = UNSET,
    ) -> ProjectMilestoneEntity:
        """Apply a partial update to one milestone, including its position.

        Reordering is `position`, and nothing else moves. The milestone cannot
        be moved to another project here; see the repository for what that
        would do to the issues pointing at it.
        """
        self._validate_milestone_fields(name=name, position=position)

        if name is UNSET and target_date is UNSET and position is UNSET:
            existing = await self._get_milestone(scope=scope, milestone_id=milestone_id)

            if existing is None:
                raise ValidationError([_MILESTONE_NOT_FOUND])

            return existing

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                updated = await self._repository.update_milestone(
                    connection,
                    scope=scope,
                    milestone_id=milestone_id,
                    set_name=name is not UNSET,
                    name=None if isinstance(name, UnsetType) else name,
                    set_target_date=target_date is not UNSET,
                    target_date=(
                        None if isinstance(target_date, UnsetType) else target_date
                    ),
                    set_position=position is not UNSET,
                    position=None if isinstance(position, UnsetType) else position,
                )

        if updated is None:
            raise ValidationError([_MILESTONE_NOT_FOUND])

        return updated

    async def delete_milestone(
        self,
        *,
        scope: WorkspaceScope,
        milestone_id: UUID,
    ) -> None:
        """Delete one milestone, leaving its issues in the project.

        `issues_milestone_fk` is RESTRICT, so the issues have to be detached
        first; that they stay in the project while doing so is the product
        decision this method encodes. One transaction, so a failure leaves the
        milestone and its issues as they were.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._issue_repository.clear_milestone(
                    connection,
                    scope=scope,
                    milestone_id=milestone_id,
                )

                deleted = await self._repository.delete_milestone(
                    connection,
                    scope=scope,
                    milestone_id=milestone_id,
                )

                if not deleted:
                    raise ValidationError([_MILESTONE_NOT_FOUND])

    async def _get_milestone(
        self,
        *,
        scope: WorkspaceScope,
        milestone_id: UUID,
    ) -> ProjectMilestoneEntity | None:
        """One milestone by id, used only by the empty-patch path.

        Written as an update of nothing rather than as a SELECT, so that the
        no-op case goes through the same statement and the same workspace
        predicate as every other update. A separate read would be a second
        definition of "is this milestone mine".
        """
        async with self._pool.acquire() as connection:
            return await self._repository.update_milestone(
                connection,
                scope=scope,
                milestone_id=milestone_id,
                set_name=False,
                name=None,
                set_target_date=False,
                target_date=None,
                set_position=False,
                position=None,
            )

    # ------------------------------------------------------------ validation

    @staticmethod
    def _validate_list(*, first: int, after: str | None) -> KeysetCursor | None:
        """Validate pagination arguments, returning the decoded cursor.

        `first` is never silently clamped, and an invalid cursor is an
        expected input error rather than a parser exception. The rules and the
        codes are IssueService's, deliberately: two list endpoints that
        disagreed about the legal page size would be a contract a client has
        to learn twice.
        """
        issues: list[ValidationIssue] = []
        cursor: KeysetCursor | None = None

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
                cursor = decode_keyset_cursor(after)
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
    def _validate_project_fields(
        *,
        name: str | UnsetType,
        description: str | None | UnsetType,
        state: str | UnsetType,
    ) -> None:
        """Collect every violation, then raise once.

        Shared by create and update, which is what keeps a project that could
        be created from being one that could not be updated back into the same
        values. A field the caller did not mention is not validated -- there
        is nothing to validate -- so UNSET short-circuits each check rather
        than being coerced into some stand-in value.

        Field order is deterministic (name, description, state) so that
        clients can rely on it. The codes and messages are a public contract.
        """
        issues: list[ValidationIssue] = []

        # Validated as supplied -- never trimmed or rewritten. A name of three
        # spaces is a name the caller typed, and silently turning it into a
        # REQUIRED failure would report an error about input the client never
        # sent.
        if not isinstance(name, UnsetType):
            if len(name) < NAME_MIN_LENGTH:
                issues.append(
                    ValidationIssue(
                        field="name",
                        code="REQUIRED",
                        message="Name is required",
                    )
                )
            elif len(name) > NAME_MAX_LENGTH:
                issues.append(
                    ValidationIssue(
                        field="name",
                        code="TOO_LONG",
                        message=f"Name must be at most {NAME_MAX_LENGTH} characters",
                    )
                )

        if (
            not isinstance(description, UnsetType)
            and description is not None
            and len(description) > DESCRIPTION_MAX_LENGTH
        ):
            issues.append(
                ValidationIssue(
                    field="description",
                    code="TOO_LONG",
                    message=(
                        f"Description must be at most {DESCRIPTION_MAX_LENGTH} "
                        "characters"
                    ),
                )
            )

        if not isinstance(state, UnsetType) and state not in PROJECT_STATES:
            # The legal values are named in the message. `projects_state_check`
            # would reject this too, but as a CheckViolationError carrying the
            # rendered constraint -- which is either masked (telling the client
            # nothing) or forwarded (telling it about the schema). Neither is
            # a usable answer to "which states may I send".
            issues.append(
                ValidationIssue(
                    field="state",
                    code="INVALID",
                    message=f"State must be one of: {', '.join(PROJECT_STATES)}",
                )
            )

        if issues:
            raise ValidationError(issues)

    @staticmethod
    def _validate_milestone_fields(
        *,
        name: str | UnsetType,
        position: int | UnsetType = UNSET,
    ) -> None:
        issues: list[ValidationIssue] = []

        if not isinstance(name, UnsetType):
            if len(name) < NAME_MIN_LENGTH:
                issues.append(
                    ValidationIssue(
                        field="name",
                        code="REQUIRED",
                        message="Name is required",
                    )
                )
            elif len(name) > NAME_MAX_LENGTH:
                issues.append(
                    ValidationIssue(
                        field="name",
                        code="TOO_LONG",
                        message=f"Name must be at most {NAME_MAX_LENGTH} characters",
                    )
                )

        if not isinstance(position, UnsetType) and position < POSITION_MIN:
            issues.append(
                ValidationIssue(
                    field="position",
                    code="OUT_OF_RANGE",
                    message=f"Position must be at least {POSITION_MIN}",
                )
            )

        if issues:
            raise ValidationError(issues)
