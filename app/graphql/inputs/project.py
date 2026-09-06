from datetime import date
from uuid import UUID

import strawberry

from app.graphql.types.project import ProjectStateType


# Every update input below spells its optional fields `T | None =
# strawberry.UNSET`, and the two halves of that mean different things.
#
# `strawberry.UNSET` as the default is what makes an omitted field arrive as
# UNSET rather than as None, which is the only way a partial update can tell
# "leave this alone" from "set this to null". The `| None` is what lets a
# client say null at all -- for `description` and `targetDate` that is how a
# value is cleared, and for `name` and `state` it is an input error the
# service reports rather than a shape the schema forbids.
#
# Making `name: String` non-nullable in the SDL would push that error into
# GraphQL validation, which sounds better and is not: `name: String!` on an
# input field with no default means *required*, so the field could no longer
# be omitted, and a partial update that only changes the state would have to
# resend the name. There is no SDL spelling for "nullable in the type system,
# rejected by the rules", so the rule lives where the other rules live.


@strawberry.input
class ProjectCreateInput:
    name: str
    description: str | None = None
    # A default here and not in the schema. migrations/009_projects.sql
    # deliberately gives `projects.state` no column default, so something has
    # to choose; the GraphQL default is visible in the SDL a client reads,
    # which a column default is not.
    state: ProjectStateType = ProjectStateType.PLANNED
    target_date: date | None = None

    # The one user id this API accepts as an argument, and the exception is
    # worth stating because the rule it bends is a real one: a client may never
    # name WHO IS ASKING -- that comes from the session, through
    # `info.context.viewer()`, and a `userId` argument standing in for it would
    # let any caller act as anyone.
    #
    # This names a different thing: a value being stored on a row, the same
    # kind of argument as `name` or `targetDate`. It is safe to accept because
    # it is not trusted -- `projects_lead_fk` refuses any id that is not a
    # member of this project's workspace, so the worst a forged one achieves is
    # a NOT_MEMBER field error. Nothing anywhere reads authorization off it.
    lead_id: UUID | None = None


@strawberry.input
class ProjectUpdateInput:
    id: UUID
    name: str | None = strawberry.UNSET
    description: str | None = strawberry.UNSET
    state: ProjectStateType | None = strawberry.UNSET
    target_date: date | None = strawberry.UNSET
    # UNSET-defaulted like the rest, and here the three cases are the whole
    # feature: omitted leaves the lead alone, an id reassigns it, and an
    # explicit null takes the lead off the project.
    lead_id: UUID | None = strawberry.UNSET


@strawberry.input
class ProjectDeleteInput:
    id: UUID


@strawberry.input
class ProjectTeamInput:
    """The argument of both team mutations.

    One input for add and remove, because the two operations name exactly the
    same pair and an asymmetry between them would be a bug rather than a
    feature: a client that can express an association it cannot express the
    removal of has a project it cannot get back out of a state.
    """

    project_id: UUID
    team_id: UUID


@strawberry.input
class ProjectMilestoneCreateInput:
    project_id: UUID
    name: str
    target_date: date | None = None


@strawberry.input
class ProjectMilestoneUpdateInput:
    id: UUID
    name: str | None = strawberry.UNSET
    target_date: date | None = strawberry.UNSET
    position: int | None = strawberry.UNSET


@strawberry.input
class ProjectMilestoneDeleteInput:
    id: UUID


@strawberry.input
class IssueSetProjectInput:
    """Where one issue sits in the plan: a project, optionally a milestone.

    Both are plain nullable fields with a null default rather than UNSET
    fields, because this is an assignment and not a patch. `projectId: null`
    is how an issue is taken out of a project, and it has to be sayable; a
    mutation that could only ever move an issue between projects would leave
    every mis-assignment permanent.

    Sending a milestone with no project is refused -- a milestone belongs to a
    project, so the pair is meaningless without one. Sending a project with no
    milestone is not: that is an issue in a project that has not been slotted
    into a phase of it.
    """

    issue_id: UUID
    project_id: UUID | None = None
    milestone_id: UUID | None = None
