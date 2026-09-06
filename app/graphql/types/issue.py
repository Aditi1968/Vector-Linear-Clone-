from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated
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


if TYPE_CHECKING:
    # Type-checking only, and `strawberry.lazy` below is what makes the
    # runtime work without it. app.graphql.types.cycle imports IssueType --
    # IssueSetCyclePayload returns one -- so importing CycleType here at
    # runtime would close the loop and neither module would import at all.
    from app.graphql.types.cycle import CycleType


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
            cycle_id=entity.cycle_id,
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
class IssueUpdatePayload:
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
