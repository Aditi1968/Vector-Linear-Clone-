from datetime import date, datetime
from enum import Enum
from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.initiatives import (
    INITIATIVE_STATUSES,
    InitiativeEntity,
    InitiativePage,
    InitiativeUpdateEntity,
)
from app.domain.tenancy import WorkspaceScope
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.health import HealthType
from app.graphql.types.pagination import PageInfo


@strawberry.enum(name="InitiativeStatus")
class InitiativeStatusType(Enum):
    """The initiative lifecycle, as an enum rather than a String.

    The same argument `ProjectStateType` makes, and deliberately a SEPARATE
    enum from it rather than a shared one: an initiative has no `paused` and a
    project has no `active`, so one type would either publish states half of
    its users may not send or force the two vocabularies to move together
    forever. `HealthType` is shared precisely because there the vocabulary
    really is one.

    The members' *values* are the strings the database stores, and the members'
    *names* are what appears in SDL.
    """

    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELED = "canceled"


# Checked at import time rather than left to a test, for the reason
# `ProjectStateType` states: a disagreement here is a payload that cannot be
# built, raised from whichever resolver reads the drifted row first, in
# production, as a masked internal error. Failing at import turns that into a
# process that will not start. An `if`/`raise` and not an `assert`, because
# `python -O` discards asserts.
if tuple(member.value for member in InitiativeStatusType) != INITIATIVE_STATUSES:
    raise RuntimeError(
        "InitiativeStatusType and app.domain.initiatives.INITIATIVE_STATUSES "
        "disagree; they are both statements of the initiatives_status_check "
        "constraint in migrations/022_initiatives.sql and have to be changed "
        "together"
    )


@strawberry.type(name="InitiativeUpdate")
class InitiativeUpdateType:
    """One posted update, as a client reads it.

    `authorId` and not `author: User`, for the reason `Project.leadId` gives:
    `UserType` carries `email`, and publishing one here would publish every
    update author's address to everyone who can read the initiative -- a
    decision about that type, made in passing by a feature branch.
    """

    id: UUID
    initiative_id: UUID
    health: HealthType
    body: str
    author_id: UUID
    created_at: datetime

    @classmethod
    def from_entity(cls, entity: InitiativeUpdateEntity) -> "InitiativeUpdateType":
        return cls(
            id=entity.id,
            initiative_id=entity.initiative_id,
            # By value: the entity carries what the column holds, and the enum
            # is keyed on exactly those strings. A row holding a health the
            # enum does not know raises ValueError here rather than being
            # rendered as something plausible.
            health=HealthType(entity.health),
            body=entity.body,
            author_id=entity.author_id,
            created_at=entity.created_at,
        )


@strawberry.type(name="Initiative")
class InitiativeType:
    id: UUID
    name: str
    description: str | None
    status: InitiativeStatusType

    # Null until somebody posts an update. Nullable in the SDL rather than
    # defaulted to ON_TRACK, because "nobody has reported" and "reported as
    # fine" are different facts and a dashboard that could not tell them apart
    # would show a wall of green for a workspace nobody is updating.
    health: HealthType | None

    target_date: date | None

    # The workspace member accountable, as an id. An id and not a `User`, for
    # the reason `Project.leadId` states at length.
    owner_id: UUID | None

    parent_initiative_id: UUID | None

    # The projects and the sub-initiatives, as ids.
    #
    # Ids and not `[Project!]!` / `[Initiative!]!`, following the argument
    # `Project.teamIds` makes: a field named `projectIds` says what it is
    # instead of pretending a resolver is coming, and when one arrives
    # `projects: [Project!]!` is added beside this and this is deprecated -- a
    # change clients can see coming. A field called `projects` returning ids
    # would have to change type to become correct, which is a breaking change
    # dressed as an improvement.
    #
    # It also keeps the fan-out honest. `initiatives(first: 100) { projects {
    # ... } }` would be a hundred project reads that app/graphql/limits.py
    # prices as one, because the field declares no page-size argument.
    project_ids: list[UUID]
    child_initiative_ids: list[UUID]

    created_at: datetime
    updated_at: datetime

    # Carried, not exposed: `strawberry.Private` keeps it out of the schema.
    #
    # The scope the root field AUTHORIZED, handed down so that a nested
    # resolver runs in the same workspace its parent was read from. It is not
    # taken from the row -- an entity that remembered its own tenant would let
    # this resolver scope a query with a value that arrived from an earlier
    # query's RESULT, which is how a compromised row becomes a key to another
    # workspace.
    #
    # Per object rather than per request, because one document may name two
    # workspaces in two root fields.
    scope: strawberry.Private[WorkspaceScope]

    @strawberry.field
    async def updates(self, info: Info) -> list[InitiativeUpdateType]:
        """This initiative's update history, newest first.

        Batched. Rendering a page of initiatives with their updates would
        otherwise issue one query per initiative, and the batch loader
        collapses them into one -- see app/graphql/loaders/initiatives.py.
        """
        entities = await info.context.initiative_updates_loader.load(
            (self.scope.workspace_id, self.id)
        )

        return [InitiativeUpdateType.from_entity(entity) for entity in entities]

    @classmethod
    def from_entity(
        cls, entity: InitiativeEntity, scope: WorkspaceScope
    ) -> "InitiativeType":
        return cls(
            scope=scope,
            id=entity.id,
            name=entity.name,
            description=entity.description,
            status=InitiativeStatusType(entity.status),
            health=None if entity.health is None else HealthType(entity.health),
            target_date=entity.target_date,
            owner_id=entity.owner_id,
            parent_initiative_id=entity.parent_initiative_id,
            project_ids=list(entity.project_ids),
            child_initiative_ids=list(entity.child_initiative_ids),
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class InitiativeConnection:
    nodes: list[InitiativeType]
    page_info: PageInfo

    @classmethod
    def from_domain(
        cls, page: InitiativePage, scope: WorkspaceScope
    ) -> "InitiativeConnection":
        return cls(
            nodes=[InitiativeType.from_entity(entity, scope) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )


# One payload type per *shape*, reused across the mutations that share it,
# rather than one per mutation -- the convention app/graphql/types/project.py
# argues for at length. The shapes below are reused while they are genuinely
# identical; the first time one of them needs a field the others do not, it
# gets its own type.
@strawberry.type
class InitiativePayload:
    initiative: InitiativeType | None
    errors: list[ValidationErrorType]


@strawberry.type
class InitiativeDeletePayload:
    # The id rather than the initiative. Returning the deleted row would invite
    # a client to render something that no longer exists; the id is what a
    # cache needs in order to evict it.
    deleted_initiative_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class InitiativeUpdatePayload:
    update: InitiativeUpdateType | None
    errors: list[ValidationErrorType]
