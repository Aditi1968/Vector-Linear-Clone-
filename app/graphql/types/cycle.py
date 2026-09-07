from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.cycles import CycleEntity
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueType


@strawberry.type(name="Cycle")
class CycleType:
    """One team's iteration, as a client sees it.

    Carries no workspace, mirroring `Issue`: publishing a tenant identifier
    invites accepting one back as an argument, which is exactly what CLAUDE.md
    forbids.

    It does carry `teamId`, and used not to. The old argument was that every
    route to a cycle already names its team, so the field would be a second
    copy of a fact the client holds -- the copy that gets trusted when the two
    disagree. That was true of `cycles(teamId:)` and false of everything
    else: a client reaching a cycle through `Issue.cycle`, or landing on a
    cycle route by id alone, holds no team, and it needs one to scope an issue
    list to that cycle's team. Without this field a cycle screen could only
    ask the whole workspace and match client-side.
    """

    id: UUID
    team_id: UUID = strawberry.field(
        description=(
            "The team this cycle belongs to. Fixed for the cycle's life -- a "
            "cycle cannot be moved between teams -- so a route or a cache may "
            "be keyed on it."
        )
    )
    number: int
    name: str | None
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity: CycleEntity) -> "CycleType":
        return cls(
            id=entity.id,
            team_id=entity.team_id,
            number=entity.number,
            name=entity.name,
            starts_at=entity.starts_at,
            ends_at=entity.ends_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class CyclePayload:
    """What `cycleCreate` and `cycleUpdate` both answer with.

    One type rather than two identical ones. Both mutations return the same
    thing -- the cycle as it now stands, or the reasons it does not -- so a
    second type would say nothing a client could act on differently, while
    giving the two contracts somewhere to drift apart.
    """

    cycle: CycleType | None
    errors: list[ValidationErrorType]


@strawberry.type
class CycleDeletePayload:
    """The id that was deleted, or why nothing was.

    The id rather than the cycle: returning the object a mutation has just
    destroyed invites a client to render it, and the id is what a cache
    needs in order to evict it.
    """

    deleted_cycle_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueSetCyclePayload:
    """The issue as it now stands, cycle included, or why it does not.

    The issue and not the cycle. The mutation changes an issue, so this is
    what a client's cache has to replace; the new cycle is reachable through
    `issue { cycle { ... } }` in the same round trip if it wants it.
    """

    issue: IssueType | None
    errors: list[ValidationErrorType]
