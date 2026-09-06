from datetime import datetime
from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.issues import IssueEntity
from app.domain.pagination import IssuePage
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo
from app.graphql.types.project import ProjectType


@strawberry.type(name="Issue")
class IssueType:
    id: UUID
    title: str
    description: str | None
    priority: int

    # Both ids are exposed alongside the `project` object below. They are not
    # redundant: a client updating a form, or evicting a cache entry, wants
    # the id it is about to send back, and paying for a whole `project`
    # selection to learn one uuid is a round trip for something the row
    # already holds.
    project_id: UUID | None
    milestone_id: UUID | None

    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @strawberry.field
    async def project(self, info: Info) -> ProjectType | None:
        """The project this issue belongs to, if any.

        Batched through a per-request DataLoader, so a page of fifty issues
        costs one query rather than fifty. See app/graphql/loaders/projects.py.

        Two nulls are deliberately indistinguishable here. An issue in no
        project resolves to null without asking anything, and an issue whose
        project is not visible in this workspace resolves to null after
        asking -- and a client cannot tell which, because telling it apart
        would answer a question about a row it may not see. The first case
        short-circuits for cost, not for semantics.

        The workspace comes from the request rather than from this object, for
        the reason ProjectType.milestones gives: a resolver must not scope a
        query with a value that arrived in an earlier query's result.
        """
        if self.project_id is None:
            return None

        scope = await info.context.tenant.scope()

        entity = await info.context.project_loader.load(
            (scope.workspace_id, self.project_id)
        )

        if entity is None:
            return None

        return ProjectType.from_entity(entity)

    @classmethod
    def from_entity(cls, entity: IssueEntity) -> "IssueType":
        return cls(
            id=entity.id,
            title=entity.title,
            description=entity.description,
            priority=entity.priority,
            project_id=entity.project_id,
            milestone_id=entity.milestone_id,
            completed_at=entity.completed_at,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )


@strawberry.type
class IssueCreatePayload:
    issue: IssueType | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueSetProjectPayload:
    """The result of moving an issue into or out of a project.

    Declared here rather than beside the other project payloads because it
    carries an `Issue`, and app/graphql/types/project.py must not import this
    module -- this one already imports it, for the `project` field above.
    """

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
