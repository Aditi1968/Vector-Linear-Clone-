from uuid import UUID

import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.relations import (
    IssueClearParentInput,
    IssueRelationCreateInput,
    IssueRelationDeleteInput,
    IssueSetParentInput,
)
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueType
from app.graphql.types.relations import IssueRelationType


@strawberry.type
class IssueParentPayload:
    """The issue whose parent changed, or the reasons it did not.

    Returns the full `Issue` rather than an `IssueSummary`, which is what
    makes this useful to a normalising client cache: `Issue` is the type the
    cache already holds under this id, so the payload updates the entity in
    place. An `IssueSummary` carries the same fields under a different
    `__typename` and would be stored as a separate object that the issue
    screen never reads.

    One payload for both setting and clearing. The two mutations differ in
    their input and in nothing else about their result.
    """

    issue: IssueType | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueRelationCreatePayload:
    """The relation as the source issue sees it, or why it was refused.

    Named from the source because that is the end the client wrote the
    request from, and it is the same reading `Issue.relations` gives when
    asked of that issue -- so a client can put this straight into the list
    it came from without translating anything.
    """

    relation: IssueRelationType | None
    errors: list[ValidationErrorType]


@strawberry.type
class IssueRelationDeletePayload:
    """The id that was removed, or why nothing was.

    The id rather than a boolean: a client updating a cached list needs to
    know which row to drop, and echoing it back means the answer comes from
    the server that did the deleting rather than from the variables the
    client happens to still have in scope.
    """

    deleted_relation_id: UUID | None
    errors: list[ValidationErrorType]


@strawberry.type
class RelationMutation:
    """Sub-issue and relation mutations.

    A separate class from the issue mutations and mixed into the root
    `Mutation` there, so that this feature's resolvers, inputs, payloads and
    service all arrive together and leave together.

    Every resolver here has the same three-line shape: resolve the tenant
    outside the try, call the service, and turn a `ValidationError` into
    `payload.errors`. Only `ValidationError` is caught. A missing workspace,
    an asyncpg failure or a bug is not something the client's input can be
    corrected to fix, and reporting one as a field error would tell a client
    to repair something that is not theirs to repair.
    """

    @strawberry.mutation
    async def issue_set_parent(
        self,
        info: Info,
        input: IssueSetParentInput,
    ) -> IssueParentPayload:
        scope = await info.context.tenant.scope()

        try:
            entity = await info.context.relation_service.set_parent(
                scope=scope,
                issue_id=input.issue_id,
                parent_id=input.parent_id,
            )
        except ValidationError as exc:
            return IssueParentPayload(
                issue=None,
                errors=[ValidationErrorType.from_domain(issue) for issue in exc.issues],
            )

        return IssueParentPayload(
            issue=IssueType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def issue_clear_parent(
        self,
        info: Info,
        input: IssueClearParentInput,
    ) -> IssueParentPayload:
        scope = await info.context.tenant.scope()

        try:
            entity = await info.context.relation_service.clear_parent(
                scope=scope,
                issue_id=input.issue_id,
            )
        except ValidationError as exc:
            return IssueParentPayload(
                issue=None,
                errors=[ValidationErrorType.from_domain(issue) for issue in exc.issues],
            )

        return IssueParentPayload(
            issue=IssueType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def issue_relation_create(
        self,
        info: Info,
        input: IssueRelationCreateInput,
    ) -> IssueRelationCreatePayload:
        scope = await info.context.tenant.scope()

        try:
            entity = await info.context.relation_service.create_relation(
                scope=scope,
                source_issue_id=input.source_issue_id,
                target_issue_id=input.target_issue_id,
                relation_type=input.type.to_domain(),
            )
        except ValidationError as exc:
            return IssueRelationCreatePayload(
                relation=None,
                errors=[ValidationErrorType.from_domain(issue) for issue in exc.issues],
            )

        return IssueRelationCreatePayload(
            relation=IssueRelationType.from_entity(entity),
            errors=[],
        )

    @strawberry.mutation
    async def issue_relation_delete(
        self,
        info: Info,
        input: IssueRelationDeleteInput,
    ) -> IssueRelationDeletePayload:
        scope = await info.context.tenant.scope()

        try:
            deleted = await info.context.relation_service.delete_relation(
                scope=scope,
                relation_id=input.id,
            )
        except ValidationError as exc:
            return IssueRelationDeletePayload(
                deleted_relation_id=None,
                errors=[ValidationErrorType.from_domain(issue) for issue in exc.issues],
            )

        return IssueRelationDeletePayload(
            deleted_relation_id=deleted,
            errors=[],
        )
