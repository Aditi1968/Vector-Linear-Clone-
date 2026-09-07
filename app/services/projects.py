from collections.abc import Sequence
from datetime import date
from typing import NoReturn
from uuid import UUID

import asyncpg

from app.domain.errors import ValidationError, ValidationIssue
from app.domain.health import HEALTH_VALUES
from app.domain.pagination import (
    InvalidCursorError,
    KeysetCursor,
    decode_keyset_cursor,
    encode_keyset_cursor,
)
from app.domain.patch import UNSET, UnsetType
from app.domain.projects import (
    PROJECT_STATES,
    ProjectDependencies,
    ProjectEntity,
    ProjectMilestoneEntity,
    ProjectPage,
    ProjectUpdateEntity,
)
from app.domain.tenancy import WorkspaceScope
from app.repositories.initiatives import InitiativeRepository
from app.repositories.issues import IssueRepository
from app.repositories.projects import ProjectRepository
from app.services.events import record_project_update


# The same alias, for the same reason, as in app/repositories/projects.py --
# `ProjectService` also has a method called `list`, which shadows the builtin
# for every annotation below it in the class body. That note has the full
# explanation, including why `from __future__ import annotations` is the wrong
# fix.
Milestones = list[ProjectMilestoneEntity]
Updates = list[ProjectUpdateEntity]


NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 200


def _name_issue(name: str | None | UnsetType) -> ValidationIssue | None:
    """The one rule for a name, for every caller that takes one.

    Three inputs, three answers, and the middle one is why this is a function
    rather than two copies of an `if`.

    UNSET means the caller did not mention the field, so there is nothing to
    validate. None means the caller explicitly sent null -- which
    `app/graphql/inputs/project.py` says at length is "an input error the
    service reports rather than a shape the schema forbids", because there is
    no SDL spelling for "nullable in the type system, rejected by the rules".
    That report was never actually written: both validators here tested only
    for UNSET and then called `len()`, so an explicit null reached `len(None)`
    and surfaced as a masked TypeError -- an internal error for what the
    schema's own comment calls ordinary bad input. A client following the
    documented contract got a 500 and no field to correct.

    None is REQUIRED rather than a code of its own. `name` is NOT NULL on both
    tables, so "clear the name" is not an operation either row has; a null and
    an empty string are the same request and deserve the same answer.
    """
    if isinstance(name, UnsetType):
        return None

    if name is None or len(name) < NAME_MIN_LENGTH:
        return ValidationIssue(
            field="name",
            code="REQUIRED",
            message="Name is required",
        )

    if len(name) > NAME_MAX_LENGTH:
        return ValidationIssue(
            field="name",
            code="TOO_LONG",
            message=f"Name must be at most {NAME_MAX_LENGTH} characters",
        )

    return None


DESCRIPTION_MAX_LENGTH = 10_000

POSITION_MIN = 0

FIRST_MIN = 1
FIRST_MAX = 100

# The most milestones one project answers with.
#
# Not a page size and not a clamp on a user-supplied argument: no caller
# chooses it, and `Project.milestones` takes none. It is the bound that keeps
# an unpaginated list reachable from a paginated one from being unbounded --
# `projects(first: 100) { milestones { ... } }` is a single document, and
# app/graphql/limits.py charges `milestones` once because it declares no
# page-size argument, so nothing above this layer prices the fan-out.
#
# Set far above any real project on purpose, exactly as MEMBERSHIP_LIST_LIMIT
# is: a plan has milestones in the low tens, so truncation is not a case real
# users reach, and the honest reading is "a backstop" rather than "page one".
# When a real project approaches it, this field grows a cursor the way
# `projects` has one; raising the number would paper over an unpaginated list
# instead of paginating it.
MILESTONE_LIST_LIMIT = 200

# The longest body a project update may carry, matching
# `project_updates_body_length` in migrations/022_initiatives.sql. Two
# statements of one rule, and they have to change together: the database has
# to refuse an oversized body whoever writes it, and this layer has to refuse
# one without turning a CheckViolationError into a user-facing message.
UPDATE_BODY_MAX_LENGTH = 10_000

# The most updates one project answers with.
#
# The same shape as MILESTONE_LIST_LIMIT and the same argument: not a page size
# and not a clamp on a user-supplied argument, but the bound that keeps an
# unpaginated list reachable from a paginated one from being unbounded.
#
# ponytail: 200 is a ceiling on history, not pagination. A weekly update for
# four years fits; a project that outgrows it silently loses its oldest
# updates from this field. The upgrade is a cursor on `Project.updates`, the
# way `projects` has one -- raising the number would paper over an unpaginated
# list instead of paginating it.
UPDATE_LIST_LIMIT = 200


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
    # The lead is not a member of this project's workspace.
    #
    # One message for three situations that must stay indistinguishable: no
    # such user anywhere, a real user who belongs to some other workspace, and
    # a real user who belongs to this one but whose membership was revoked
    # between the client reading the member list and sending this. Naming which
    # would let anyone holding a workspace answer "is this person a member of
    # yours?" for any user id they can guess, and the id space is not the
    # secret -- the membership is.
    #
    # NOT_MEMBER rather than NOT_FOUND, because the two say different things to
    # a client that has both a user picker and a member list: this is the one
    # field error a UI can act on by refreshing the list it drew the value
    # from.
    "projects_lead_fk": ValidationIssue(
        field="leadId",
        code="NOT_MEMBER",
        message="Lead must be a member of this workspace",
    ),
    "project_updates_project_fk": ValidationIssue(
        field="projectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    # The author is not a member of this project's workspace.
    #
    # NOT_MEMBER rather than NOT_FOUND, for the reason projects_lead_fk gives,
    # and reachable in one ordinary situation rather than only through a forged
    # request: a session outliving the membership it was created under. The
    # viewer is real and the project is real, and posting is still refused.
    "project_updates_author_fk": ValidationIssue(
        field="authorId",
        code="NOT_MEMBER",
        message="Author must be a member of this workspace",
    ),
    # Which end of the dependency is missing is named, because a client can
    # only correct the half it got wrong. What is deliberately NOT
    # distinguished is the tenant: a project in another workspace and a project
    # that does not exist both break the same key and both produce NOT_FOUND,
    # so this mutation cannot be used to ask whether an id exists somewhere the
    # caller cannot see.
    "project_dependencies_blocking_fk": ValidationIssue(
        field="blockingProjectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    "project_dependencies_blocked_fk": ValidationIssue(
        field="blockedProjectId",
        code="NOT_FOUND",
        message="Project not found",
    ),
    "project_dependencies_pkey": ValidationIssue(
        field="blockedProjectId",
        code="DUPLICATE",
        message="That dependency already exists",
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

# Reported against `blockedProjectId` rather than the blocking half, because
# that is the field the client is most likely to have mis-picked: the blocking
# project is the subject of the mutation and the blocked one is the argument.
_SELF_DEPENDENCY = ValidationIssue(
    field="blockedProjectId",
    code="SELF_DEPENDENCY",
    message="A project cannot block itself",
)

# The multi-step cycle, which no constraint can see -- see the block at the top
# of migrations/022_initiatives.sql. `CYCLE` is the code RelationService
# already publishes for the sub-issue version of this refusal, reused rather
# than invented so a client learns one vocabulary for one concept.
_DEPENDENCY_CYCLE = ValidationIssue(
    field="blockedProjectId",
    code="CYCLE",
    message="That project already blocks this one, directly or indirectly",
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
        initiative_repository: InitiativeRepository,
    ):
        self._pool = pool
        self._repository = repository

        # Deleting a project has to detach the issues that point at it, and
        # SQL against `issues` belongs to the repository that owns that table.
        # A service reaching across to a second repository inside one
        # transaction is the shape this architecture is for; a project
        # repository writing to `issues` would not be.
        self._issue_repository = issue_repository

        # And the initiatives it belongs to, for exactly the same reason:
        # `initiative_projects_project_fk` is RESTRICT, so those rows have to
        # go first, and the SQL against that table belongs to the repository
        # that owns it.
        self._initiative_repository = initiative_repository

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
    ) -> Milestones:
        """One project's milestones, in display order.

        A project in another workspace, and one that does not exist, both
        produce an empty list -- the same answer a real project with no
        milestones gives. Nothing here reveals which of the three it was.

        Bounded by MILESTONE_LIST_LIMIT, which the service states rather than
        the repository defaulting to.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list_milestones(
                connection,
                scope=scope,
                project_id=project_id,
                limit=MILESTONE_LIST_LIMIT,
            )

    async def list_milestones_for_projects(
        self,
        *,
        scope: WorkspaceScope,
        # `Sequence`, not `list`: the name means the method here, as the note
        # on `Milestones` above explains. It reads better anyway -- this only
        # iterates the ids.
        project_ids: Sequence[UUID],
    ) -> Milestones:
        """Several projects' milestones at once, for batching.

        The same MILESTONE_LIST_LIMIT, applied PER PROJECT rather than to the
        batch. A limit over the whole result would make one project's
        milestones depend on how many other projects happened to be on the
        same page -- a project would render completely on its own and
        truncated in a list, which is a difference no client could explain.
        """
        if not project_ids:
            return []

        async with self._pool.acquire() as connection:
            return await self._repository.list_milestones_for_projects(
                connection,
                scope=scope,
                project_ids=project_ids,
                limit_per_project=MILESTONE_LIST_LIMIT,
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
        lead_id: UUID | None = None,
    ) -> ProjectEntity:
        """Create one project in this workspace, associated with no teams yet.

        Teams are a separate call rather than a list on this one. Associating
        a team can fail on its own terms -- the team may not exist, or may
        belong to another workspace -- and folding those failures into
        creation would mean either abandoning the project over one bad team id
        or reporting a partial success that no payload shape describes well.

        The lead IS on this call, and the asymmetry with teams is not an
        inconsistency: a lead is a column on the row being inserted, so it
        succeeds or fails with the insert and there is no partial state to
        report. Nothing here checks that the lead is a member first --
        `projects_lead_fk` is composite over the workspace, so the same
        statement that writes the row is what refuses a non-member, with no
        window between the two for a membership to be revoked in.
        """
        self._validate_project_fields(name=name, description=description, state=state)

        async with self._pool.acquire() as connection:
            # The service owns the transaction boundary: later this block
            # will also carry the audit / sync / outbox writes.
            async with connection.transaction():
                try:
                    return await self._repository.create(
                        connection,
                        scope=scope,
                        name=name,
                        description=description,
                        state=state,
                        target_date=target_date,
                        lead_id=lead_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    # Only projects_lead_fk can fire here -- it is the one
                    # foreign key on this INSERT whose value came from a
                    # client. projects_workspace_fk breaking would mean the
                    # request resolved a workspace that no longer exists, which
                    # is not something the caller can correct, so `_raise_mapped`
                    # re-raises it rather than reporting it as bad input.
                    _raise_mapped(error)

    async def update(
        self,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        name: str | None | UnsetType = UNSET,
        description: str | None | UnsetType = UNSET,
        state: str | UnsetType = UNSET,
        target_date: date | None | UnsetType = UNSET,
        lead_id: UUID | None | UnsetType = UNSET,
    ) -> ProjectEntity:
        """Apply a partial update and return the project as it now stands.

        `UNSET` and `None` are different arguments: UNSET leaves a field
        alone, None clears it. `name` and `state` cannot be cleared -- the
        columns are NOT NULL -- so their types admit no None at all, and a
        client that sends null for either is refused by the GraphQL layer
        before this is called.

        `lead_id` is the field that needs all three cases most: leaving the
        lead alone while renaming a project, handing it to someone else, and
        taking it off a project entirely are three different intentions, and a
        signature that could not tell the first from the third would silently
        unassign a lead on every rename.

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
            and lead_id is UNSET
        ):
            existing = await self.get_by_id(scope=scope, project_id=project_id)

            if existing is None:
                raise ValidationError([_PROJECT_NOT_FOUND])

            return existing

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
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
                        set_lead_id=lead_id is not UNSET,
                        lead_id=None if isinstance(lead_id, UnsetType) else lead_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

        if updated is None:
            raise ValidationError([_PROJECT_NOT_FOUND])

        return updated

    async def delete(self, *, scope: WorkspaceScope, project_id: UUID) -> None:
        """Delete one project and everything that hangs off it.

        The detachment is written out rather than delegated to ON DELETE
        CASCADE, and the order is the order the foreign keys require:

            issues -> milestones -> team links -> updates -> dependencies
            -> initiative links -> the project itself

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

                # The update history goes with the project rather than being
                # kept, unlike the issues above. An update is a statement
                # ABOUT this project and about nothing else; keeping it would
                # leave rows describing the health of something that no longer
                # exists, and there is no other project they could be moved to.
                await self._repository.clear_updates(
                    connection,
                    scope=scope,
                    project_id=project_id,
                )
                # Both ends in one statement -- a project may be blocking and
                # blocked, and clearing one direction at a time would leave the
                # second statement to fail on the half already gone.
                await self._repository.clear_dependencies(
                    connection,
                    scope=scope,
                    project_id=project_id,
                )
                await self._initiative_repository.clear_project_links(
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
        name: str | None | UnsetType = UNSET,
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

    # --------------------------------------------------------------- updates

    async def post_update(
        self,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
        health: str,
        body: str,
        author_id: UUID,
    ) -> ProjectUpdateEntity:
        """Record how a project is going, and stamp the project with it.

        TWO writes in ONE transaction, and that is the whole method. The row in
        `project_updates` is the history; `projects.health` is the current
        value the board renders. migrations/022_initiatives.sql argues at
        length for storing both, and names this transaction as the thing that
        keeps them in step -- so a failure between the two must roll both back,
        not leave a project claiming a health nothing in its history reports.

        The order matters as well as the atomicity. The insert runs FIRST
        because it is the statement carrying the foreign keys: a project from
        another workspace and an author who is not a member are both refused
        there, before anything has stamped a row. Doing the UPDATE first would
        write a health onto a project and then discover the author was not
        entitled to report it.

        `author_id` is not taken from the client. It arrives from the resolver
        as the session's user, which is the difference between "who says so"
        and "who a request claims says so"; `project_updates_author_fk` is the
        second half of that and refuses an id that is not a member here,
        whatever this code passes.

        THREE writes, then, once the domain events are counted -- and their
        position between the other two is load-bearing rather than tidy.
        `record_project_update` decides whether the health MOVED by comparing
        against the stored value, so it has to run before `set_health` stamps
        the new one; running it afterwards would compare the new value with
        itself and announce a change never, for anybody. Inside the same
        transaction, so a rolled-back update announces nothing.

        Nothing about Slack is reachable from here. This module records that a
        project was updated; whether any workspace wants that announced, where,
        and what happens when Slack refuses are decided by
        `app.services.notifications`, reading the rows this leaves behind.
        """
        self._validate_update_fields(health=health, body=body)

        async with self._pool.acquire() as connection:
            async with connection.transaction():
                try:
                    entity = await self._repository.create_update(
                        connection,
                        scope=scope,
                        project_id=project_id,
                        health=health,
                        body=body,
                        author_id=author_id,
                    )
                except asyncpg.ForeignKeyViolationError as error:
                    _raise_mapped(error)

                # Before `set_health`, and see the docstring: the health event
                # is decided by comparing against the value still stored.
                await record_project_update(
                    connection,
                    scope=scope,
                    project_id=project_id,
                    health=health,
                    body=body,
                )

                await self._repository.set_health(
                    connection,
                    scope=scope,
                    project_id=project_id,
                    health=health,
                )

        return entity

    async def list_updates(
        self,
        *,
        scope: WorkspaceScope,
        project_id: UUID,
    ) -> Updates:
        """One project's update history, newest first.

        A project in another workspace, and one that does not exist, both
        produce an empty list -- the same answer a real project nobody has
        posted about gives. Nothing here reveals which of the three it was.
        """
        async with self._pool.acquire() as connection:
            return await self._repository.list_updates(
                connection,
                scope=scope,
                project_id=project_id,
                limit=UPDATE_LIST_LIMIT,
            )

    async def list_updates_for_projects(
        self,
        *,
        scope: WorkspaceScope,
        project_ids: Sequence[UUID],
    ) -> Updates:
        """Several projects' updates at once, for batching.

        The same UPDATE_LIST_LIMIT, applied PER PROJECT rather than to the
        batch, for the reason `list_milestones_for_projects` states.
        """
        if not project_ids:
            return []

        async with self._pool.acquire() as connection:
            return await self._repository.list_updates_for_projects(
                connection,
                scope=scope,
                project_ids=project_ids,
                limit_per_project=UPDATE_LIST_LIMIT,
            )

    # ---------------------------------------------------------- dependencies

    async def add_dependency(
        self,
        *,
        scope: WorkspaceScope,
        blocking_project_id: UUID,
        blocked_project_id: UUID,
    ) -> ProjectDependencies:
        """Record that one project blocks another, and return both directions.

        Three refusals, in the order they become knowable:

        * A project cannot block itself. This is a comparison of two arguments
          -- it reads no row, so there is nothing to race, and it is settled
          before a connection is taken. `project_dependencies_not_self` says
          the same thing in the database and remains the guarantee; this only
          produces the better message.
        * The dependency must not close a loop. That is the cycle guard, and
          unlike everything else here it is enforced by this code rather than
          by a constraint -- no CHECK may read a second row. It runs inside the
          transaction, after the workspace's dependency lock, so no concurrent
          write can invalidate the reachability it read.
        * Both projects must exist in this workspace, and the edge must not
          already be there. Not checked here at all: the composite foreign keys
          and the primary key refuse those, and the refusals are translated
          into the field that named the offending id.

        Cross-workspace is not among the checks this code performs, and that is
        the point: `project_dependencies` holds ONE workspace_id read by both
        foreign keys, so PostgreSQL is what refuses a project from another
        tenant, as one statement, with nothing in between for a concurrent move
        to exploit.
        """
        if blocking_project_id == blocked_project_id:
            raise ValidationError([_SELF_DEPENDENCY])

        async with self._pool.acquire() as connection:
            # The transaction is load-bearing rather than conventional: the
            # advisory lock below is released at its end, and it must not be
            # released until the write it protects has committed.
            async with connection.transaction():
                await self._repository.lock_dependencies(connection, scope=scope)

                if await self._repository.depends_on(
                    connection,
                    scope=scope,
                    from_project_id=blocked_project_id,
                    to_project_id=blocking_project_id,
                ):
                    raise ValidationError([_DEPENDENCY_CYCLE])

                try:
                    await self._repository.add_dependency(
                        connection,
                        scope=scope,
                        blocking_project_id=blocking_project_id,
                        blocked_project_id=blocked_project_id,
                    )
                except (
                    asyncpg.ForeignKeyViolationError,
                    asyncpg.UniqueViolationError,
                ) as error:
                    _raise_mapped(error)

                found = await self._repository.list_dependencies_for_projects(
                    connection,
                    scope=scope,
                    project_ids=[blocking_project_id],
                )

        return found[blocking_project_id]

    async def remove_dependency(
        self,
        *,
        scope: WorkspaceScope,
        blocking_project_id: UUID,
        blocked_project_id: UUID,
    ) -> ProjectDependencies:
        """Drop one dependency, and return the blocking project's remaining.

        An edge that was not there, a project in another workspace and a
        project that never existed are all answered the same way: the delete
        matches nothing and the reload reports what is left. Removal is not a
        failure when there was nothing to remove, because the caller's intent
        -- this project does not block that one -- already holds.

        No lock and no cycle check: removing an edge cannot close a loop.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await self._repository.remove_dependency(
                    connection,
                    scope=scope,
                    blocking_project_id=blocking_project_id,
                    blocked_project_id=blocked_project_id,
                )

                found = await self._repository.list_dependencies_for_projects(
                    connection,
                    scope=scope,
                    project_ids=[blocking_project_id],
                )

        return found[blocking_project_id]

    async def list_dependencies_for_projects(
        self,
        *,
        scope: WorkspaceScope,
        project_ids: Sequence[UUID],
    ) -> dict[UUID, ProjectDependencies]:
        """Both directions for several projects, for batching.

        Unpaginated, and bounded by the caller's list rather than by a constant
        of its own: `project_ids` is already a page, and a project has a
        handful of dependencies rather than a history.
        """
        if not project_ids:
            return {}

        async with self._pool.acquire() as connection:
            return await self._repository.list_dependencies_for_projects(
                connection,
                scope=scope,
                project_ids=project_ids,
            )

    # ------------------------------------------------------------ validation

    @staticmethod
    def _validate_update_fields(*, health: str, body: str) -> None:
        """Collect every violation, then raise once.

        The legal healths are named in the message. `project_updates_health_check`
        would reject an unknown one too, but as a CheckViolationError carrying
        the rendered constraint -- which is either masked (telling the client
        nothing) or forwarded (telling it about the schema). Neither is a
        usable answer to "which values may I send".
        """
        issues: list[ValidationIssue] = []

        if health not in HEALTH_VALUES:
            issues.append(
                ValidationIssue(
                    field="health",
                    code="INVALID",
                    message=f"Health must be one of: {', '.join(HEALTH_VALUES)}",
                )
            )

        # Validated as supplied -- never trimmed or rewritten, matching
        # `_validate_project_fields`. A body of three spaces is what the caller
        # typed, and silently turning it into a REQUIRED failure would report
        # an error about input the client never sent.
        if len(body) < 1:
            issues.append(
                ValidationIssue(
                    field="body",
                    code="REQUIRED",
                    message="Body is required",
                )
            )
        elif len(body) > UPDATE_BODY_MAX_LENGTH:
            issues.append(
                ValidationIssue(
                    field="body",
                    code="TOO_LONG",
                    message=(
                        f"Body must be at most {UPDATE_BODY_MAX_LENGTH} characters"
                    ),
                )
            )

        if issues:
            raise ValidationError(issues)

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
        name: str | None | UnsetType,
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
        name_issue = _name_issue(name)

        if name_issue is not None:
            issues.append(name_issue)

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
        name: str | None | UnsetType,
        position: int | UnsetType = UNSET,
    ) -> None:
        issues: list[ValidationIssue] = []

        name_issue = _name_issue(name)

        if name_issue is not None:
            issues.append(name_issue)

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
