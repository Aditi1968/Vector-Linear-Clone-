from datetime import datetime
from uuid import UUID

import strawberry

from app.domain.issues import IssueEntity
from app.domain.pagination import IssuePage
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo


@strawberry.type(name="Issue")
class IssueType:
    id: UUID
    title: str
    description: str | None
    priority: int
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

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
