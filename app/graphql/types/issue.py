from datetime import datetime
from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.domain.issues import IssueEntity
from app.domain.pagination import IssuePage
from app.graphql.errors import bad_user_input
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.pagination import PageInfo
from app.graphql.types.relations import (
    DEFAULT_RELATED_FIRST,
    IssueRelationConnection,
    IssueSummaryConnection,
    IssueSummaryType,
)


@strawberry.type(name="Issue")
class IssueType:
    id: UUID
    title: str
    description: str | None
    priority: int
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    # --- edges ---------------------------------------------------------
    #
    # Three resolved fields, all returning types that cannot lead back to an
    # `Issue`. See `IssueSummaryType` for why that is a property of the
    # schema rather than of the depth limit.
    #
    # Each costs one query per issue it is asked of, and none is batched:
    # `app/graphql/loaders/` is still empty, so a page of issues selecting
    # `children` really does issue one statement per row. What bounds that
    # today is `app.graphql.limits.MAX_COMPLEXITY`, which prices a nested
    # list at (outer size x inner size x fields below) -- so a document
    # asking a page of issues for their sub-issues is refused above roughly
    # twenty parents rather than allowed to fan out unbounded. That is a
    # bound, not a solution; a DataLoader keyed on (workspace, parent id) is
    # the solution, and it belongs here when a screen needs it.

    @strawberry.field
    async def parent(self, info: Info) -> IssueSummaryType | None:
        """The issue this one is a sub-issue of, if any.

        Null covers three cases a client cannot tell apart: no parent, no
        such issue, and a parent in another workspace. The last is the
        point -- an id that resolves to null says nothing about whether it
        exists somewhere the caller cannot see.
        """
        scope = await info.context.tenant.scope()

        entity = await info.context.relation_service.find_parent(
            scope=scope,
            issue_id=self.id,
        )

        if entity is None:
            return None

        return IssueSummaryType.from_entity(entity)

    @strawberry.field
    async def children(
        self,
        info: Info,
        first: int = DEFAULT_RELATED_FIRST,
        after: str | None = None,
    ) -> IssueSummaryConnection:
        """This issue's sub-issues, newest first.

        A connection rather than a plain list, so that the page size is a
        number the operation-limit rule can read and charge for. An
        unbounded `[IssueSummary!]!` would be priced at one however many
        rows it returned, which is how a single field ends up handing back
        every sub-issue of a thousand-child parent inside a document that
        measured as cheap.

        Sub-issues in other TEAMS of this workspace are included: that is
        the product rule migration 010's `issues_parent_fk` is shaped for,
        and a team filter here would quietly take it back.
        """
        scope = await info.context.tenant.scope()

        try:
            page = await info.context.relation_service.list_children(
                scope=scope,
                parent_id=self.id,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return IssueSummaryConnection.from_domain(page)

    @strawberry.field
    async def relations(
        self,
        info: Info,
        first: int = DEFAULT_RELATED_FIRST,
        after: str | None = None,
    ) -> IssueRelationConnection:
        """Every relation this issue has, in both directions, newest first.

        One list, not two. A relation is stored once and read from either
        end, so this issue's `BLOCKS` rows and its `BLOCKED_BY` rows come
        out of one page in one order; a client wanting only one kind filters
        on `type`.
        """
        scope = await info.context.tenant.scope()

        try:
            page = await info.context.relation_service.list_relations(
                scope=scope,
                issue_id=self.id,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return IssueRelationConnection.from_domain(page)

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
