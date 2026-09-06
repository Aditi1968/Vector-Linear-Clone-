from datetime import datetime
from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.domain.issues import IssueEntity
from app.domain.pagination import IssuePage
from app.graphql.types.comment import (
    DEFAULT_COMMENT_FIRST,
    CommentConnection,
)
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.label import LabelType
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

    @strawberry.field
    async def labels(self, info: Info) -> list[LabelType]:
        """This issue's labels, alphabetically.

        Unpaginated, and bounded instead at the write: an issue may carry at
        most `app.services.labels.LABELS_PER_ISSUE_MAX` labels, which is a ceiling
        imposed when a label is attached rather than a truncation applied when
        one is read. Truncating here would silently hide labels an issue
        really wears, which is a worse answer than refusing the attach that
        went over.

        The cost this field is CHARGED is 1 (app/graphql/limits.py prices a
        field with no `first`/`last` argument as a single selection), so on a
        page of issues its true cardinality is under-priced by up to that
        ceiling. What keeps that from being a fan-out of queries is the
        DataLoader below: one statement per page of issues, not one per issue.

        The workspace comes from the request and is part of the loader key, so
        two issues with the same id in two workspaces -- which cannot happen
        today and would not need to for this to matter -- could never share a
        cached answer.
        """
        scope = await info.context.tenant.scope()

        entities = await info.context.issue_labels.load((scope.workspace_id, self.id))

        return [LabelType.from_entity(entity) for entity in entities]

    @strawberry.field
    async def comments(
        self,
        info: Info,
        first: int = DEFAULT_COMMENT_FIRST,
        after: str | None = None,
    ) -> CommentConnection:
        """This issue's comments, oldest first.

        Paginated rather than batched, which is the opposite choice from
        `labels` above and rests on the same reasoning read the other way. A
        DataLoader batches a WHOLE list per key; a PAGE per key cannot be
        batched without a lateral join, and it does not have to be: because
        this field declares `first`, the complexity rule prices it properly,
        so a document asking for comments across a large page of issues is
        refused during validation instead of being served as a fan-out. See
        DEFAULT_COMMENT_FIRST for the arithmetic.

        The workspace comes from the request, never from the document, so this
        cannot be used to read another tenant's discussion even with a
        correct issue id -- the page comes back empty, exactly as it does for
        an issue nobody has commented on.
        """
        scope = await info.context.tenant.scope()

        try:
            page = await info.context.comment_service.list_for_issue(
                scope=scope,
                issue_id=self.id,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            # Only expected pagination input errors are translated. Anything
            # else (asyncpg failures, bugs) propagates as a real execution
            # error. `from None` keeps parser detail out of the response.
            raise GraphQLError(
                "Invalid pagination arguments",
                extensions={
                    "code": "BAD_USER_INPUT",
                    "issues": [
                        {
                            "field": issue.field,
                            "code": issue.code,
                            "message": issue.message,
                        }
                        for issue in exc.issues
                    ],
                },
            ) from None

        return CommentConnection.from_domain(page)


@strawberry.type
class IssueCreatePayload:
    issue: IssueType | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueLabelPayload:
    """The answer to issueLabelAttach and issueLabelDetach.

    Carries the issue rather than the association, so a client can re-select
    `issue { labels { ... } }` in the same round trip and see the result of
    the change instead of inferring it.
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
