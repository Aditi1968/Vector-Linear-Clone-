from datetime import date, datetime
from uuid import UUID

import strawberry

from app.domain.issues import IssueEntity
from app.domain.pagination import IssuePage
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo


@strawberry.type(name="Issue")
class IssueType:
    id: UUID
    team_id: UUID

    identifier: str = strawberry.field(
        description=(
            "The name this issue is known by outside the product -- ENG-42. "
            "Its team's key, a hyphen, and the issue's number."
        )
    )
    number: int = strawberry.field(
        description=(
            "Sequential within the team and never reused. Unique only "
            "alongside the team; two teams both have a number 42."
        )
    )

    title: str
    description: str | None
    priority: int

    workflow_state_id: UUID
    assignee_id: UUID | None
    creator_id: UUID | None = strawberry.field(
        description=(
            "Who filed the issue, or null where that is not known -- an issue "
            "created before accounts existed, or one whose author's account "
            "has since been deleted."
        )
    )

    estimate: int | None
    due_date: date | None = strawberry.field(
        description=(
            "A calendar day, not an instant: the same day for every viewer, "
            "in every timezone."
        )
    )

    completed_at: datetime | None = strawberry.field(
        description=(
            "When the issue stopped being worked on. Derived from the "
            "workflow state's category and not settable directly: it is "
            "non-null exactly while the issue sits in a completed or "
            "canceled state."
        )
    )
    archived_at: datetime | None = strawberry.field(
        description=(
            "When the issue was taken off the board. Always null here, "
            "because archived issues are absent from every query -- only the "
            "archive mutation's own result carries a value."
        )
    )

    created_at: datetime
    updated_at: datetime

    # `workspace_id` is deliberately absent, matching TeamType. Publishing a
    # tenant identifier invites accepting one back as an argument, which is
    # exactly what CLAUDE.md forbids: a client names a workspace by slug and
    # the server resolves it.
    @classmethod
    def from_entity(cls, entity: IssueEntity) -> "IssueType":
        return cls(
            id=entity.id,
            team_id=entity.team_id,
            identifier=entity.identifier,
            number=entity.number,
            title=entity.title,
            description=entity.description,
            priority=entity.priority,
            workflow_state_id=entity.workflow_state_id,
            assignee_id=entity.assignee_id,
            creator_id=entity.creator_id,
            estimate=entity.estimate,
            due_date=entity.due_date,
            completed_at=entity.completed_at,
            archived_at=entity.archived_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class IssueCreatePayload:
    issue: IssueType | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueUpdatePayload:
    issue: IssueType | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueArchivePayload:
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
