import strawberry
from strawberry.types import Info

from app.domain.errors import ValidationError
from app.graphql.inputs.bulk import IssueBulkArchiveInput, IssueBulkUpdateInput
from app.graphql.scope import authorized_scope
from app.graphql.types.bulk import IssueBulkPayload
from app.graphql.types.errors import ValidationErrorType


def _errors(exc: ValidationError) -> list[ValidationErrorType]:
    """The domain's structured issues, as the transport reports them."""
    return [ValidationErrorType.from_domain(issue) for issue in exc.issues]


@strawberry.type
class BulkMutation:
    """Multi-issue mutations, merged into the root Mutation.

    Two resolvers, both three lines, and everything interesting is one layer
    down. The property worth stating here is the one a reader of the SCHEMA
    should be able to rely on: neither of these has a partial outcome. A batch
    naming a hundred issues either changes all hundred or changes none, and the
    payload's empty `issues` beside a non-empty `errors` is how "none" is
    reported. `BulkService` explains what makes that true -- one transaction,
    and a count of the ids that resolved in the caller's workspace, compared
    inside it.

    In particular, an id belonging to another workspace does not silently drop
    out of the batch. It fails the batch, and the message names no id: a caller
    who could learn WHICH of a hundred ids was rejected could binary-search
    another tenant's issue ids at a hundred per request.
    """

    @strawberry.mutation
    async def issue_bulk_update(
        self, info: Info, input: IssueBulkUpdateInput
    ) -> IssueBulkPayload:
        """Apply one change to a selection of issues, or to none of them.

        A workflow state, a cycle or a project that does not fit EVERY issue in
        the selection refuses the whole batch. That is the honest answer rather
        than a limitation: `issues_workflow_state_fk` ties a state to one team,
        so "move these to In Progress" over a selection spanning two teams
        names a state most of them cannot be in, and moving the ones that fit
        would be a different request from the one that was sent.
        """
        # Resolved before the try, and outside it. An unauthenticated caller,
        # and one who may not see this workspace, are not things the client's
        # INPUT can be corrected to fix, so they leave as GraphQL errors rather
        # than as field errors on the payload.
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entities = await info.context.bulk_service.update_many(
                scope=scope,
                issue_ids=input.issue_ids,
                patch=input.to_patch(),
                add_label_ids=input.add_label_ids,
                remove_label_ids=input.remove_label_ids,
                actor_id=scope.user_id,
            )
        except ValidationError as exc:
            # Only expected input validation is translated into the payload.
            # Everything else (asyncpg failures, bugs, outages) propagates
            # through GraphQL's normal error mechanism and is masked.
            return IssueBulkPayload.refused(_errors(exc))

        return IssueBulkPayload.of(entities)

    @strawberry.mutation
    async def issue_bulk_archive(
        self, info: Info, input: IssueBulkArchiveInput
    ) -> IssueBulkPayload:
        """Take a selection of issues off the board, or none of them.

        The archived issues are returned so a client can render the change from
        this result. It is the last time any query in the product will hand
        those rows back.
        """
        scope = await authorized_scope(info, input.workspace_slug)

        try:
            entities = await info.context.bulk_service.archive_many(
                scope=scope,
                issue_ids=input.issue_ids,
                actor_id=scope.user_id,
            )
        except ValidationError as exc:
            return IssueBulkPayload.refused(_errors(exc))

        return IssueBulkPayload.of(entities)
