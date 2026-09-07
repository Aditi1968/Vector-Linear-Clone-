from datetime import date, datetime
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

import strawberry
from graphql import GraphQLError
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.domain.issues import (
    NO_FILTER,
    IssueEntity,
    IssueFilter,
    IssueOrderField,
    OrderDirection,
)
from app.domain.pagination import IssuePage
from app.domain.tenancy import WorkspaceScope
from app.graphql.errors import bad_user_input
from app.graphql.types.activity import (
    DEFAULT_ACTIVITY_FIRST,
    IssueActivityConnection,
)
from app.graphql.types.comment import (
    DEFAULT_COMMENT_FIRST,
    CommentConnection,
)
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.github import GithubDevelopmentType
from app.graphql.types.label import LabelType
from app.graphql.types.pagination import PageInfo
from app.graphql.types.project import ProjectType
from app.graphql.types.relations import (
    DEFAULT_RELATED_FIRST,
    IssueRelationConnection,
    IssueSummaryConnection,
    IssueSummaryType,
)


if TYPE_CHECKING:
    # Type-checking only, and `strawberry.lazy` below is what makes the
    # runtime work without it. app.graphql.types.cycle imports IssueType --
    # IssueSetCyclePayload returns one -- so importing CycleType here at
    # runtime would close the loop and neither module would import at all.
    from app.graphql.types.cycle import CycleType


# The domain enums, published rather than restated -- the same move
# `app/graphql/types/team.py` makes for WorkflowStateCategory, and for the
# same reason: two enums of strings that must agree drift in a way that
# type-checks. `strawberry.enum` annotates the class it is given and returns
# it, so the dependency still points one way and `app/domain/issues.py`
# imports nothing from strawberry.
IssueOrderFieldType = strawberry.enum(
    IssueOrderField,
    name="IssueOrderField",
    description=(
        "What an issue list is sorted by. PRIORITY is urgency and not the "
        "raw column: 0 means no priority rather than the lowest one, so "
        "ascending is Urgent, High, Medium, Low and then the untriaged. "
        "DUE_DATE ascending puts the soonest first and the undated last."
    ),
)

OrderDirectionType = strawberry.enum(
    OrderDirection,
    name="OrderDirection",
    description="Which end of an ordering a list starts from.",
)


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

    # Both ids are exposed alongside the `project` object below. They are not
    # redundant: a client updating a form, or evicting a cache entry, wants
    # the id it is about to send back, and paying for a whole `project`
    # selection to learn one uuid is a round trip for something the row
    # already holds.
    project_id: UUID | None
    milestone_id: UUID | None

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

    # Carried, not exposed. The scope the root field AUTHORIZED, handed down
    # so that `cycle`, `project`, `parent`, `children`, `relations`, `labels`
    # and `comments` all run in the workspace this issue was read from.
    #
    # It comes from `app.graphql.scope.authorized_scope` by way of the
    # resolver that built this object -- never from the row. An entity that
    # remembered its own tenant would let these resolvers scope their queries
    # with a value that arrived in an earlier query's RESULT rather than from
    # the request, and one document may legally name two workspaces, so no
    # single per-request value could serve them either.
    scope: strawberry.Private[WorkspaceScope]

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

        entity = await info.context.cycle_loader.load(
            scope=self.scope,
            cycle_id=self.cycle_id,
        )

        if entity is None:
            return None

        # Imported here rather than at module scope: see the note above the
        # TYPE_CHECKING block. This runs per resolved issue, and the import
        # is a dict lookup in sys.modules after the first one.
        from app.graphql.types.cycle import CycleType

        return CycleType.from_entity(entity)

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

        The workspace is the one this issue was read under -- see `scope`
        above -- so a project reached from here can only ever be one the
        caller was already authorized for.
        """
        if self.project_id is None:
            return None

        entity = await info.context.project_loader.load(
            (self.scope.workspace_id, self.project_id)
        )

        if entity is None:
            return None

        return ProjectType.from_entity(entity, self.scope)

    @strawberry.field
    async def parent(self, info: Info) -> IssueSummaryType | None:
        """The issue this one is a sub-issue of, if any.

        Null covers three cases a client cannot tell apart: no parent, no
        such issue, and a parent in another workspace. The last is the
        point -- an id that resolves to null says nothing about whether it
        exists somewhere the caller cannot see.
        """
        entity = await info.context.relation_service.find_parent(
            scope=self.scope,
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
        try:
            page = await info.context.relation_service.list_children(
                scope=self.scope,
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
        try:
            page = await info.context.relation_service.list_relations(
                scope=self.scope,
                issue_id=self.id,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return IssueRelationConnection.from_domain(page)

    @strawberry.field
    async def activity(
        self,
        info: Info,
        first: int = DEFAULT_ACTIVITY_FIRST,
        after: str | None = None,
    ) -> IssueActivityConnection:
        """What has happened to this issue, newest first.

        Not `comments` and not a merged feed of the two. A comment is what
        somebody wrote and can withdraw; this is what the system recorded
        happening, and it is append-only. A client that wants them
        interleaved selects both and merges on `createdAt`, which is a
        rendering decision and belongs where the rendering is.

        Paginated rather than batched, for the reason `comments` gives: a
        page per key cannot be batched without a lateral join, and because
        this field declares `first`, the complexity rule prices it properly
        instead of letting a document buy a fan-out that measured as cheap.

        The workspace comes from the request, never from the document, so
        this cannot read another tenant's history even with a correct issue
        id -- the page comes back empty, exactly as it does for an issue
        nothing has happened to.
        """
        try:
            page = await info.context.activity_service.list_for_issue(
                # The scope this issue was resolved under, like every sibling
                # resolver here. It used to come from a per-request "current
                # tenant" on the context; that object is gone, because a scope
                # a resolver can reach without having been authorized for it
                # is a scope a resolver can forget to authorize.
                scope=self.scope,
                issue_id=self.id,
                first=first,
                after=after,
            )
        except ValidationError as exc:
            raise bad_user_input("Invalid pagination arguments", exc) from None

        return IssueActivityConnection.from_domain(page)

    @strawberry.field
    async def development(self, info: Info) -> GithubDevelopmentType:
        """The pull requests and commits attached to this issue.

        Hung off the issue rather than published as a root field, and that is
        the authorization decision rather than a shape preference: `self.scope`
        is the scope the root resolver already authorised this issue under, so
        there is no second boundary to get right and no `issueId` argument for
        a caller to aim at another workspace. A root field would need both.

        Deliberately NOT admin-only, unlike `githubIntegration`. Connecting a
        GitHub organisation is an admin's act and is guarded as one; the pull
        requests attached to an issue are part of the issue, and every member
        who can open it can see them.

        Non-null, and it answers for an issue with no activity rather than
        returning null: `branchName` is what that issue's panel renders, so an
        absence here would hide the field's only useful state.

        Not batched. `Issue.labels` and `Issue.cycle` go through DataLoaders
        because they are selected on every row of a fifty-issue page; this
        belongs to the inspector for one open issue, and a loader for a field
        selected once per document is a cache with nothing to batch.
        """
        entity = await info.context.github_service.development_for_issue(
            self.scope,
            issue_id=self.id,
            # Both come from the row this type was built from, so the branch
            # name is derived from what the issue IS rather than from anything
            # the document sent.
            identifier=self.identifier,
            title=self.title,
        )

        return GithubDevelopmentType.from_entity(entity)

    @classmethod
    def from_entity(cls, entity: IssueEntity, scope: WorkspaceScope) -> "IssueType":
        return cls(
            scope=scope,
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
            project_id=entity.project_id,
            milestone_id=entity.milestone_id,
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

        The workspace is part of the loader key, so two issues with the same
        id in two workspaces -- which cannot happen today and would not need
        to for this to matter -- could never share a cached answer.
        """
        entities = await info.context.issue_labels.load(
            (self.scope.workspace_id, self.id)
        )

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

        The workspace is the authorized one this issue was read under, so
        this cannot be used to read another tenant's discussion even with a
        correct issue id -- the page comes back empty, exactly as it does for
        an issue nobody has commented on.
        """
        try:
            page = await info.context.comment_service.list_for_issue(
                scope=self.scope,
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

    # Carried, not exposed: what `totalCount` has to count, and the workspace
    # it counts inside. Both come from the resolver that authorized the
    # request, never from the document, so the aggregate cannot be steered
    # somewhere the page itself was not read from.
    scope: strawberry.Private[WorkspaceScope]
    issue_filter: strawberry.Private[IssueFilter]

    @strawberry.field(
        description=(
            "How many live issues match, ignoring paging -- the number a "
            "column header states, as opposed to how many have been loaded."
        )
    )
    async def total_count(self, info: Info) -> int:
        """A second aggregate over the same predicate as the page.

        A resolver and not a field on the page, which is the whole cost
        story: the count is one more query per issue list, so a document that
        does not select it does not run it. Worth paying when the number is
        the answer -- a board column that must say how much work is in a
        state, a filter chip reporting what it would select -- and not worth
        paying for an infinite scroll, where `pageInfo.hasNextPage` already
        says whether to fetch again and costs nothing extra.

        Not memoised against the page either. Two selections of `totalCount`
        in one document are two selections of a field with no arguments, and
        the operation limits already price and cap that.
        """
        total: int = await info.context.issue_service.count(
            scope=self.scope,
            issue_filter=self.issue_filter,
        )

        return total

    @classmethod
    def from_domain(
        cls,
        page: IssuePage,
        scope: WorkspaceScope,
        issue_filter: IssueFilter = NO_FILTER,
    ) -> "IssueConnection":
        return cls(
            nodes=[IssueType.from_entity(entity, scope) for entity in page.nodes],
            page_info=PageInfo(
                has_next_page=page.has_next_page,
                end_cursor=page.end_cursor,
            ),
            scope=scope,
            issue_filter=issue_filter,
        )
