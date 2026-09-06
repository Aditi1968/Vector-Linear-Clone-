from datetime import datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.issues import IssueEntity
from app.domain.pagination import IssuePage
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo


if TYPE_CHECKING:
    # Type-checking only, and `strawberry.lazy` below is what makes the
    # runtime work without it. app.graphql.types.cycle imports IssueType --
    # IssueSetCyclePayload returns one -- so importing CycleType here at
    # runtime would close the loop and neither module would import at all.
    from app.graphql.types.cycle import CycleType


@strawberry.type(name="Issue")
class IssueType:
    id: UUID
    title: str
    description: str | None
    priority: int
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    # Carried, not exposed: `strawberry.Private` keeps it out of the schema.
    # The `cycle` resolver below needs the id to fetch with, and a client
    # that wanted the id alone can read `cycle { id }` -- publishing both
    # would be two spellings of one fact, and the flat one is the one that
    # gets used to skip the tenant-scoped lookup.
    cycle_id: strawberry.Private[UUID | None]

    @strawberry.field
    async def cycle(
        self,
        info: Info,
    ) -> Annotated["CycleType", strawberry.lazy("app.graphql.types.cycle")] | None:
        """The cycle this issue is in, or null.

        Batched through the request's CycleLoader rather than fetched here,
        so that a page of issues costs one statement instead of one per
        issue. An issue in no cycle costs nothing at all: it never reaches
        the loader.

        Null also covers a cycle the request's workspace cannot see. That is
        unreachable through the schema -- `issues_cycle_fk` guarantees an
        issue's cycle is its own team's, and the issue was itself read under
        this scope -- so it is a floor rather than an expected answer: if the
        two ever disagree, this resolves to null instead of handing back a
        row from outside the scope that asked.
        """
        if self.cycle_id is None:
            return None

        scope = await info.context.tenant.scope()

        entity = await info.context.cycle_loader.load(
            scope=scope,
            cycle_id=self.cycle_id,
        )

        if entity is None:
            return None

        # Imported here rather than at module scope: see the note above the
        # TYPE_CHECKING block. This runs per resolved issue, and the import
        # is a dict lookup in sys.modules after the first one.
        from app.graphql.types.cycle import CycleType

        return CycleType.from_entity(entity)

    @classmethod
    def from_entity(cls, entity: IssueEntity) -> "IssueType":
        return cls(
            id=entity.id,
            title=entity.title,
            description=entity.description,
            priority=entity.priority,
            completed_at=entity.completed_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
            cycle_id=entity.cycle_id,
        )


@strawberry.type
class IssueCreatePayload:
    issue: IssueType | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueConnection:
    nodes: list[IssueType]
    page_info: PageInfo

    @classmethod
    def from_domain(cls, page: IssuePage) -> "IssueConnection":
        return cls(
            nodes=[IssueType.from_entity(entity) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
        )
