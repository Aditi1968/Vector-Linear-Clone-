import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.domain.tenancy import WorkspaceScope
from app.graphql.inputs.label import (
    IssueLabelInput,
    LabelCreateInput,
    LabelDeleteInput,
    LabelUpdateInput,
)
from app.graphql.types.errors import ValidationErrorType
from app.graphql.types.issue import IssueLabelPayload, IssueType
from app.graphql.types.label import LabelDeletePayload, LabelPayload, LabelType


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    """The domain's structured issues, as the transport reports them."""
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class LabelMutation:
    """The write half of labels, merged into the root Mutation.

    Every resolver here is the same three lines: resolve the workspace, call
    the service, and turn the one expected exception into a payload. Nothing
    decides anything -- an id that names nothing, a duplicate name, a label
    already applied are all rules the service and the schema own between them,
    and a resolver that re-checked any of them would be a second copy that can
    disagree with the first.

    None of these require an authenticated viewer, which matches `issueCreate`
    and is the same gap for the same reason: the workspace itself still comes
    from `app/graphql/tenancy.py` rather than from a request that proved
    anything. `commentCreate` is the exception, and deliberately so -- a
    comment records WHO wrote it, so it cannot be written without an identity
    at all. When workspace authorization lands it lands on all of these
    together; adding half of it here would read as a rule that is enforced.
    """

    @strawberry.mutation
    async def label_create(self, info: Info, input: LabelCreateInput) -> LabelPayload:
        # Resolved before the try, and outside it. A missing workspace is not
        # something the client's input can be corrected to fix, so catching it
        # here would report a server-side gap as a field error.
        scope = await info.context.tenant.scope()

        try:
            entity = await info.context.label_service.create(
                scope=scope,
                name=input.name,
                color=input.color,
            )
        except ValidationError as exc:
            # Only expected input validation is translated into the payload.
            # Everything else (asyncpg failures, bugs, outages) propagates
            # through GraphQL's normal error mechanism and is masked.
            return LabelPayload(label=None, errors=_errors(exc))

        return LabelPayload(label=LabelType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def label_update(self, info: Info, input: LabelUpdateInput) -> LabelPayload:
        scope = await info.context.tenant.scope()

        try:
            entity = await info.context.label_service.update(
                scope=scope,
                label_id=input.id,
                name=input.name,
                color=input.color,
            )
        except ValidationError as exc:
            return LabelPayload(label=None, errors=_errors(exc))

        return LabelPayload(label=LabelType.from_entity(entity), errors=[])

    @strawberry.mutation
    async def label_delete(
        self, info: Info, input: LabelDeleteInput
    ) -> LabelDeletePayload:
        """Delete a label, and with it every issue's use of it.

        `issue_labels_label_fk ON DELETE CASCADE` removes the associations. No
        issue is touched: an association carries no data of its own, and
        deleting a label is exactly how a workspace stops using one.
        """
        scope = await info.context.tenant.scope()

        try:
            deleted_id = await info.context.label_service.delete(
                scope=scope,
                label_id=input.id,
            )
        except ValidationError as exc:
            return LabelDeletePayload(deleted_label_id=None, errors=_errors(exc))

        return LabelDeletePayload(deleted_label_id=deleted_id, errors=[])

    @strawberry.mutation
    async def issue_label_attach(
        self, info: Info, input: IssueLabelInput
    ) -> IssueLabelPayload:
        """Apply a label to an issue, both in this request's workspace.

        The payload carries the ISSUE rather than the association, so a client
        can re-select `issue { labels { ... } }` in the same round trip and see
        the result instead of inferring it. That read costs one statement after
        the write; the alternative is a client that updates its cache from what
        it hoped the server did.
        """
        scope = await info.context.tenant.scope()

        try:
            await info.context.label_service.attach(
                scope=scope,
                issue_id=input.issue_id,
                label_id=input.label_id,
            )
        except ValidationError as exc:
            return IssueLabelPayload(issue=None, errors=_errors(exc))

        return await self._issue_payload(info, scope, input)

    @strawberry.mutation
    async def issue_label_detach(
        self, info: Info, input: IssueLabelInput
    ) -> IssueLabelPayload:
        scope = await info.context.tenant.scope()

        try:
            await info.context.label_service.detach(
                scope=scope,
                issue_id=input.issue_id,
                label_id=input.label_id,
            )
        except ValidationError as exc:
            return IssueLabelPayload(issue=None, errors=_errors(exc))

        return await self._issue_payload(info, scope, input)

    @staticmethod
    async def _issue_payload(
        info: Info,
        scope: WorkspaceScope,
        input: IssueLabelInput,
    ) -> IssueLabelPayload:
        """Re-read the issue the change landed on.

        Read back rather than reconstructed, so the payload reports the row as
        it now is. `issue` is nullable for the error case above and stays
        nullable here for a reason that is not merely typing: the issue can be
        deleted between the write and this read, and answering null is honest
        where inventing a stale object would not be.
        """
        entity = await info.context.issue_service.get_by_id(
            scope=scope,
            issue_id=input.issue_id,
        )

        return IssueLabelPayload(
            issue=None if entity is None else IssueType.from_entity(entity),
            errors=[],
        )
