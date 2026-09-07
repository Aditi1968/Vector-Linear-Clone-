from datetime import date, datetime
from enum import Enum
from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.projects import (
    PROJECT_STATES,
    ProjectDependencies,
    ProjectEntity,
    ProjectMilestoneEntity,
    ProjectPage,
    ProjectUpdateEntity,
)
from app.domain.tenancy import WorkspaceScope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.health import HealthType
from app.graphql.types.pagination import PageInfo


@strawberry.enum(name="ProjectState")
class ProjectStateType(Enum):
    """The project lifecycle, as an enum rather than a String.

    An enum is the difference between a client discovering the legal values by
    reading the schema and discovering them by sending one and being told no.
    It also moves the check to GraphQL validation, which runs before any
    resolver, so an unknown state never reaches a service or a connection.

    The members' *values* are the strings the database stores, and the
    members' *names* are what appears in SDL -- so the wire contract is
    `PLANNED` while the column holds `planned`. Keeping both spellings in one
    place is what stops the mapping being an `if` ladder somewhere.

    Not generated from `PROJECT_STATES`: a dynamically built enum has no names
    for a type checker or an editor to know about, and this is a contract that
    should be greppable. tests/test_graphql_projects.py asserts the two agree,
    which is the check a generated enum would have made unnecessary and a
    hand-written one makes cheap.
    """

    PLANNED = "planned"
    STARTED = "started"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELED = "canceled"


# Checked at import time rather than left to a test. The enum above and
# `PROJECT_STATES` are two spellings of `projects_state_check`, and a
# disagreement between them is not a failing feature -- it is a payload that
# cannot be built, raised from whichever resolver happens to read the drifted
# row first, in production, as a masked internal error. Failing at import turns
# that into a process that will not start.
#
# An `if`/`raise` and not an `assert`, because `python -O` discards asserts and
# this is a startup gate rather than a debugging aid.
if tuple(member.value for member in ProjectStateType) != PROJECT_STATES:
    raise RuntimeError(
        "ProjectStateType and app.domain.projects.PROJECT_STATES disagree; "
        "they are both statements of the projects_state_check constraint in "
        "migrations/009_projects.sql and have to be changed together"
    )


@strawberry.type(name="ProjectMilestone")
class ProjectMilestoneType:
    id: UUID
    project_id: UUID
    name: str
    target_date: date | None
    position: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: ProjectMilestoneEntity) -> "ProjectMilestoneType":
        return cls(
            id=entity.id,
            project_id=entity.project_id,
            name=entity.name,
            target_date=entity.target_date,
            position=entity.position,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type(name="ProjectUpdate")
class ProjectUpdateType:
    """One posted update on a project, as a client reads it.

    `authorId` and not `author: User`, for the reason `Project.leadId` gives
    below.
    """

    id: UUID
    project_id: UUID
    health: HealthType
    body: str
    author_id: UUID
    created_at: datetime

    @classmethod
    def from_entity(cls, entity: ProjectUpdateEntity) -> "ProjectUpdateType":
        return cls(
            id=entity.id,
            project_id=entity.project_id,
            health=HealthType(entity.health),
            body=entity.body,
            author_id=entity.author_id,
            created_at=entity.created_at,
        )


@strawberry.type(name="ProjectDependencies")
class ProjectDependenciesType:
    """One project's dependencies, named from that project's point of view.

    Two id lists rather than a list of edges carrying a direction, because the
    direction is not something a client should have to interpret: the table
    stores one row per edge and never its inverse, so `blockedBy` comes into
    existence here rather than in the database.

    Ids rather than `[Project!]!`, for the reason `Project.teamIds` gives -- and
    with the extra force that resolving them would make a page of projects a
    page of project reads that app/graphql/limits.py prices as one.
    """

    blocks: list[UUID]
    blocked_by: list[UUID]

    @classmethod
    def from_domain(cls, domain: ProjectDependencies) -> "ProjectDependenciesType":
        return cls(
            blocks=list(domain.blocks),
            blocked_by=list(domain.blocked_by),
        )


@strawberry.type(name="Project")
class ProjectType:
    id: UUID
    name: str
    description: str | None
    state: ProjectStateType

    # Null until somebody posts an update, and nullable in the SDL rather than
    # defaulted: "nobody has reported" and "reported as fine" are different
    # facts, and a board that could not tell them apart would show a wall of
    # green for a workspace nobody is updating.
    #
    # A separate axis from `state`, not a finer grain of it: `state` is where
    # the project is in its lifecycle, `health` is whether it is going well. A
    # started project may be off track and a paused one may be fine.
    health: HealthType | None

    target_date: date | None

    # The workspace member accountable for this project, as an id.
    #
    # An id and not a `User`, for a reason `UserType`'s own docstring states:
    # it carries `email`, and today the only field returning one is `me`, so
    # the only address any caller can read is their own. Exposing a `lead:
    # User` here would publish every project lead's email address to everyone
    # who can read the project -- a decision about that type, made in passing
    # by a feature branch. When a User does become reachable from other
    # people's data, `lead: User` is added beside this field and this one is
    # deprecated; a field named `leadId` can do that without changing type,
    # which is the same argument the `teamIds` note below makes.
    lead_id: UUID | None

    # The teams this project spans, as ids.
    #
    # Ids and not `[Team!]!`, because this schema has no Team type yet. Naming
    # the field `teamIds` says what it is instead of pretending a resolver is
    # coming: when teams reach the API, `teams: [Team!]!` is added beside this
    # and this one is deprecated, which is a change clients can see coming. A
    # field called `teams` returning ids would have to change type to become
    # correct, and that is a breaking change dressed as an improvement.
    team_ids: list[UUID]

    created_at: datetime
    updated_at: datetime

    # Carried, not exposed: `strawberry.Private` keeps it out of the schema.
    #
    # The scope the root field AUTHORIZED, handed down so that a nested
    # resolver runs in the same workspace its parent was read from. It is not
    # taken from the row -- an entity that remembered its own tenant would let
    # this resolver scope a query with a value that arrived from an earlier
    # query's RESULT, which is how a compromised row becomes a key to another
    # workspace. It travels from `authorized_scope`, which is the only thing
    # that produces one, through the resolver that built this object.
    #
    # Per object rather than per request, because one document may name two
    # workspaces in two root fields; a single ambient scope would serve the
    # second field's children out of the first field's tenant.
    scope: strawberry.Private[WorkspaceScope]

    @strawberry.field
    async def milestones(self, info: Info) -> list[ProjectMilestoneType]:
        """This project's milestones, in display order.

        Batched. Rendering a page of projects with their milestones would
        otherwise issue one query per project, and the batch loader collapses
        them into one -- see app/graphql/loaders/projects.py.
        """
        entities = await info.context.project_milestones_loader.load(
            (self.scope.workspace_id, self.id)
        )

        return [ProjectMilestoneType.from_entity(entity) for entity in entities]

    @strawberry.field
    async def updates(self, info: Info) -> list[ProjectUpdateType]:
        """This project's update history, newest first.

        Batched, for the reason `milestones` is: a page of projects with their
        updates would otherwise issue one query per project.
        """
        entities = await info.context.project_updates_loader.load(
            (self.scope.workspace_id, self.id)
        )

        return [ProjectUpdateType.from_entity(entity) for entity in entities]

    @strawberry.field
    async def dependencies(self, info: Info) -> ProjectDependenciesType:
        """What this project blocks, and what blocks it.

        Batched, and one field rather than two so that both directions cost one
        batch: they come out of one statement in the repository, and two fields
        would either issue it twice or need a loader each.

        Total: a project with no dependencies -- and a key naming a project in
        another workspace -- resolves to two empty lists rather than null, so a
        client walking a stale list of ids gets empty answers, not a failed
        query.
        """
        domain = await info.context.project_dependencies_loader.load(
            (self.scope.workspace_id, self.id)
        )

        return ProjectDependenciesType.from_domain(domain)

    @classmethod
    def from_entity(cls, entity: ProjectEntity, scope: WorkspaceScope) -> "ProjectType":
        return cls(
            scope=scope,
            id=entity.id,
            name=entity.name,
            description=entity.description,
            # By value: the entity carries what the column holds, and the enum
            # is keyed on exactly those strings. A row holding a state the
            # enum does not know raises ValueError here rather than being
            # rendered as something plausible -- which is the right failure,
            # since the only way to store one is a migration that widened
            # `projects_state_check` without widening this.
            state=ProjectStateType(entity.state),
            # Null stays null: it is the real state "nobody has reported yet",
            # not a value to substitute for.
            health=None if entity.health is None else HealthType(entity.health),
            target_date=entity.target_date,
            lead_id=entity.lead_id,
            team_ids=list(entity.team_ids),
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class ProjectConnection:
    nodes: list[ProjectType]
    page_info: PageInfo

    @classmethod
    def from_domain(
        cls, page: ProjectPage, scope: WorkspaceScope
    ) -> "ProjectConnection":
        return cls(
            nodes=[ProjectType.from_entity(entity, scope) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


# One payload type per *shape*, reused across the mutations that share it,
# rather than one per mutation.
#
# The convention of a distinct payload per mutation exists so that a field can
# be added for one operation without appearing on the others. That is a real
# benefit and it is not free: four types differing only in name are four places
# to add the next common field and four chances to add it to three of them. The
# shapes below are reused while they are genuinely identical; the first time
# one of them needs a field the others do not, it gets its own type.
@strawberry.type
class ProjectPayload:
    project: ProjectType | None
    errors: list[ValidationErrorType]


@strawberry.type
class ProjectDeletePayload:
    # The id rather than the project. Returning the deleted row would invite a
    # client to render something that no longer exists; the id is what a cache
    # needs in order to evict it.
    deleted_project_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class ProjectMilestonePayload:
    milestone: ProjectMilestoneType | None
    errors: list[ValidationErrorType]


@strawberry.type
class ProjectMilestoneDeletePayload:
    deleted_milestone_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class ProjectUpdatePayload:
    update: ProjectUpdateType | None
    errors: list[ValidationErrorType]


@strawberry.type
class ProjectDependencyPayload:
    # The dependencies as they now stand, from the BLOCKING project's side --
    # which is the subject of both mutations. Returning the whole set rather
    # than the one edge is what lets a client re-render without a second query,
    # and it is the only shape that says something useful for a removal, where
    # there is no edge left to return.
    dependencies: ProjectDependenciesType | None
    errors: list[ValidationErrorType]
