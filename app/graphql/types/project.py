from datetime import date, datetime
from enum import Enum
from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.projects import (
    PROJECT_STATES,
    ProjectEntity,
    ProjectMilestoneEntity,
    ProjectPage,
)
from app.graphql.types.errors import ValidationErrorType
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


@strawberry.type(name="Project")
class ProjectType:
    id: UUID
    name: str
    description: str | None
    state: ProjectStateType
    target_date: date | None

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

    @strawberry.field
    async def milestones(self, info: Info) -> list[ProjectMilestoneType]:
        """This project's milestones, in display order.

        Batched. Rendering a page of projects with their milestones would
        otherwise issue one query per project, and the batch loader collapses
        them into one -- see app/graphql/loaders/projects.py.

        The workspace comes from the request rather than from this object.
        `ProjectType` carries no workspace and must not: an entity that
        remembered its tenant would let a resolver scope a query with a value
        that arrived from an earlier query's result instead of from the
        request.
        """
        scope = await info.context.tenant.scope()

        entities = await info.context.project_milestones_loader.load(
            (scope.workspace_id, self.id)
        )

        return [ProjectMilestoneType.from_entity(entity) for entity in entities]

    @classmethod
    def from_entity(cls, entity: ProjectEntity) -> "ProjectType":
        return cls(
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
            target_date=entity.target_date,
            team_ids=list(entity.team_ids),
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class ProjectConnection:
    nodes: list[ProjectType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: ProjectPage) -> "ProjectConnection":
        return cls(
            nodes=[ProjectType.from_entity(entity) for entity in page.nodes],
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
