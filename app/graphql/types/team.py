from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.estimates import EstimateScale
from app.domain.teams import (
    TeamWorkflow,
    WorkflowStateCategory,
    WorkflowStateEntity,
)
from app.graphql.types.errors import ValidationErrorType


# The domain enum, published as a GraphQL enum rather than restated as one.
#
# `strawberry.enum` annotates the class it is given and returns it, so this
# adds a GraphQL definition to the domain enum from the GraphQL layer; the
# dependency still points one way, and `app/domain/teams.py` imports nothing
# from strawberry. Declaring a second five-member enum here would be the
# alternative, and the two would drift the first time a category was added --
# in a way that type-checks, since both are enums of strings.
#
# GraphQL enum values are the member NAMES, so clients see BACKLOG, UNSTARTED,
# STARTED, COMPLETED, CANCELED while the database stores the lowercase values.
WorkflowStateCategoryType = strawberry.enum(
    WorkflowStateCategory,
    name="WorkflowStateCategory",
    description="What a workflow state means, independent of what it is called.",
)


# The estimate scale, published rather than restated, for the same reason and
# by the same mechanism as the category above: two enums of strings that must
# agree drift in a way that type-checks, and `strawberry.enum` annotates the
# class it is given and returns it, so `app/domain/estimates.py` still imports
# nothing from strawberry.
EstimateScaleType = strawberry.enum(
    EstimateScale,
    name="EstimateScale",
    description=(
        "What a team's estimates count. NONE is a whole number with no unit "
        "named, which is what every estimate written before this setting "
        "existed means. POINTS and HOURS are units and put no ceiling on the "
        "value. TSHIRT is a LADDER rather than a quantity: the stored integer "
        "is a position, 1 through 5, rendered XS, S, M, L, XL -- so a team on "
        "that scale can only write those five numbers."
    ),
)


@strawberry.type(
    name="WorkflowState",
    description="A status an issue can occupy on one team's board.",
)
class WorkflowStateType:
    id: UUID
    name: str
    category: WorkflowStateCategory = strawberry.field(
        description=(
            "The fixed category this state belongs to. Branch on this, never "
            "on the name, which the team owns and may change."
        )
    )
    position: int
    color: str | None

    @classmethod
    def from_entity(cls, entity: WorkflowStateEntity) -> "WorkflowStateType":
        return cls(
            id=entity.id,
            name=entity.name,
            category=entity.category,
            position=entity.position,
            color=entity.color,
        )


@strawberry.type(name="Team")
class TeamType:
    id: UUID
    key: str = strawberry.field(
        description=(
            "The prefix of this team's issue identifiers -- the ENG in ENG-42. "
            "Unique within the workspace, and not beyond it."
        )
    )
    name: str
    estimate_scale: EstimateScale = strawberry.field(
        description=(
            "The unit this team's estimates are in. Read it to LABEL an "
            "`Issue.estimate` -- the number alone says nothing, which is what "
            "this field exists to fix -- and to decide which values an "
            "estimate input may offer. An issue's scale is its team's; there "
            "is no per-issue override."
        )
    )
    created_at: datetime
    workflow_states: list[WorkflowStateType]

    @classmethod
    def from_domain(cls, workflow: TeamWorkflow) -> "TeamType":
        return cls(
            id=workflow.team.id,
            key=workflow.team.key,
            name=workflow.team.name,
            estimate_scale=workflow.team.estimate_scale,
            created_at=workflow.team.created_at,
            workflow_states=[
                WorkflowStateType.from_entity(state)
                for state in workflow.workflow_states
            ],
        )


# `workspace_id` is deliberately absent from TeamType, and its absence is a
# decision rather than an oversight.
#
# Publishing it would put a tenant identifier in every client's hands, and the
# obvious next step -- accepting it back as an argument -- is precisely what
# CLAUDE.md forbids: the server must never trust a workspace id supplied by the
# frontend. A client addresses a workspace by slug and the server resolves it;
# nothing downstream needs the id to have made the round trip.


@strawberry.type
class TeamPayload:
    """The result of creating a team.

    The team comes back with its `workflowStates` already populated, because
    a team without them cannot hold an issue and a client that created one is
    about to draw its board. See `TeamService.create`: both are written in one
    transaction, so there is no moment at which this payload could honestly
    report a team with an empty board.
    """

    team: TeamType | None
    errors: list[ValidationErrorType]
